#!/usr/bin/env python3
"""Canonical to Charmed Kubernetes Team Reconciler.

Zero-dependency Python 3.12 script using standard library urllib to synchronize
teams and memberships between GitHub organizations.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
import re
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


class GitHubError(Exception):
    """Exception raised for GitHub API errors with sanitized details."""

    def __init__(self, status: int, method: str, url: str, message: str) -> None:
        self.status = status
        self.method = method
        self.url = url
        self.message = message
        super().__init__(f"GitHubError {status} {method} {url}: {message}")


def _parse_next_link(link_header: str | None) -> str | None:
    """Parse RFC 5988 Link header to find rel="next" URL, handling commas inside URLs."""
    if not link_header:
        return None
    for match in re.finditer(r'<([^>]+)>(.*?)(?:,|$)', link_header):
        url = match.group(1)
        params = match.group(2)
        rel_match = re.search(r'rel=(?:"([^"]+)"|([^\s;]+))', params)
        if rel_match:
            rel = rel_match.group(1) or rel_match.group(2)
            if rel.lower() == "next":
                return url
    return None


class GitHubClient:
    """Minimal GitHub REST API client using standard library urllib."""

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.github.com",
        opener: urllib.request.OpenerDirector | None = None,
        sleep_fn: Any = time.sleep,
        timeout: float = 30.0,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.opener = opener or urllib.request.build_opener()
        self.sleep_fn = sleep_fn
        self.timeout = timeout

    def _sanitize(self, text: str) -> str:
        if self.token and self.token in text:
            return text.replace(self.token, "[REDACTED]")
        return text

    def request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, Any, dict[str, str]]:
        """Execute an HTTP request with JSON payload, retrying transient errors."""
        method = method.upper()
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = f"{self.base_url}/{path.lstrip('/')}"

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self.token}",
        }

        data: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")

        max_retries = 3
        for attempt in range(max_retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self.opener.open(req, timeout=self.timeout) as resp:
                    status = resp.status if hasattr(resp, "status") else 200
                    resp_headers = dict(resp.info().items()) if hasattr(resp, "info") else {}
                    raw_body = resp.read()
                    parsed_data = None
                    if raw_body:
                        try:
                            parsed_data = json.loads(raw_body.decode("utf-8"))
                        except Exception:
                            parsed_data = raw_body.decode("utf-8", errors="replace")
                    return status, parsed_data, resp_headers
            except urllib.error.HTTPError as e:
                status = e.code
                if status in (502, 503, 504) and attempt < max_retries:
                    backoff = 1.0 * (2 ** attempt)
                    self.sleep_fn(backoff)
                    continue

                error_message = ""
                try:
                    error_bytes = e.read()
                    if error_bytes:
                        try:
                            err_json = json.loads(error_bytes.decode("utf-8"))
                            error_message = err_json.get("message", error_bytes.decode("utf-8", errors="replace"))
                        except Exception:
                            error_message = error_bytes.decode("utf-8", errors="replace")
                except Exception:
                    error_message = str(e.reason)

                sanitized_msg = self._sanitize(error_message or str(e.reason))
                sanitized_url = self._sanitize(url)
                raise GitHubError(status, method, sanitized_url, sanitized_msg) from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < max_retries:
                    backoff = 1.0 * (2 ** attempt)
                    self.sleep_fn(backoff)
                    continue
                err_detail = str(e.reason) if hasattr(e, "reason") else str(e)
                sanitized_msg = self._sanitize(err_detail)
                sanitized_url = self._sanitize(url)
                raise GitHubError(0, method, sanitized_url, sanitized_msg) from None

        raise GitHubError(0, method, self._sanitize(url), "Retries exhausted")

    def paginate(self, path: str) -> list[dict[str, Any]]:
        """Paginate through all pages using RFC 5988 Link headers."""
        items: list[dict[str, Any]] = []
        next_path: str | None = path

        while next_path:
            status, page_items, headers = self.request_json("GET", next_path)
            if isinstance(page_items, list):
                items.extend(page_items)
            elif isinstance(page_items, dict):
                items.append(page_items)

            link_header = None
            for k, v in headers.items():
                if k.lower() == "link":
                    link_header = v
                    break
            next_path = _parse_next_link(link_header)

        return items


@dataclass
class TeamAction:
    """Represents a discrete reconciliation action taken or planned for a user."""

    action: str
    username: str
    role: str | None = None
    detail: str = ""


@dataclass
class TeamSyncResult:
    """Represents the outcome of synchronizing a single team."""

    team_slug: str
    created_team: bool = False
    actions: list[TeamAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    desired_count: int = 0


def reconcile_team(
    source_client: GitHubClient,
    target_client: GitHubClient,
    source_org: str,
    target_org: str,
    team_slug: str,
    target_org_owners: set[str],
    *,
    dry_run: bool = False,
    allow_empty: bool = False,
) -> TeamSyncResult:
    """Reconcile a single team between source and target organizations."""
    src_org_q = urllib.parse.quote(source_org, safe="")
    tgt_org_q = urllib.parse.quote(target_org, safe="")
    team_q = urllib.parse.quote(team_slug, safe="")

    # 1. Read source memberships (member and maintainer)
    source_members = source_client.paginate(f"/orgs/{src_org_q}/teams/{team_q}/members?role=member&per_page=100")
    source_maintainers = source_client.paginate(f"/orgs/{src_org_q}/teams/{team_q}/members?role=maintainer&per_page=100")
    desired: dict[str, tuple[str, str]] = {}
    for m in source_members:
        login = m["login"]
        desired[login.lower()] = (login, "member")
    for m in source_maintainers:
        login = m["login"]
        # Maintainer wins if user appears in both
        desired[login.lower()] = (login, "maintainer")

    # Circuit breaker check: abort before any target mutations if source is empty
    if len(desired) == 0 and not allow_empty:
        raise RuntimeError(
            f"Circuit breaker tripped: source team @{source_org}/{team_slug} returned 0 members and --allow-empty was not specified."
        )

    actions: list[TeamAction] = []
    errors: list[str] = []
    created_team = False

    # 2. Check if target team exists
    target_active: dict[str, tuple[str, str]] = {}
    target_pending: dict[str, dict[str, Any]] = {}

    target_team_exists = True
    try:
        target_client.request_json("GET", f"/orgs/{tgt_org_q}/teams/{team_q}")
    except GitHubError as e:
        if e.status == 404:
            target_team_exists = False
        else:
            raise

    if not target_team_exists:
        if dry_run:
            created_team = True
        else:
            try:
                target_client.request_json(
                    "POST",
                    f"/orgs/{tgt_org_q}/teams",
                    {
                        "name": team_slug,
                        "privacy": "closed",
                        "description": f"Synced from @{source_org}/{team_slug}",
                    },
                )
                created_team = True
            except GitHubError as e:
                errors.append(f"Failed to create target team {team_slug}: {e.message}")
                return TeamSyncResult(
                    team_slug=team_slug,
                    created_team=False,
                    actions=actions,
                    errors=errors,
                    desired_count=len(desired),
                )
    else:
        # 3. Read target active members and pending invitations
        active_members = target_client.paginate(f"/orgs/{tgt_org_q}/teams/{team_q}/members?role=member&per_page=100")
        active_maintainers = target_client.paginate(f"/orgs/{tgt_org_q}/teams/{team_q}/members?role=maintainer&per_page=100")
        for m in active_members:
            login = m["login"]
            target_active[login.lower()] = (login, "member")
        for m in active_maintainers:
            login = m["login"]
            target_active[login.lower()] = (login, "maintainer")

        pending_invitations = target_client.paginate(f"/orgs/{tgt_org_q}/teams/{team_q}/invitations?per_page=100")
        for inv in pending_invitations:
            login = inv.get("login")
            if login is not None:
                target_pending[login.lower()] = inv

    owners_lower = {o.lower() for o in target_org_owners}

    # 4. Process desired members (add / update role / noop / skip pending)
    for login_lower, (display_login, desired_role) in sorted(desired.items()):
        user_q = urllib.parse.quote(display_login, safe="")
        if login_lower in target_active:
            active_display, active_role = target_active[login_lower]
            if active_role == desired_role:
                actions.append(TeamAction("noop", display_login, role=desired_role))
            elif login_lower in owners_lower and desired_role == "member" and active_role == "maintainer":
                actions.append(
                    TeamAction(
                        "noop_org_owner",
                        display_login,
                        role=desired_role,
                        detail="org owner coerced to maintainer by GitHub",
                    )
                )
            else:
                if dry_run:
                    actions.append(TeamAction("update_role", display_login, role=desired_role))
                else:
                    try:
                        target_client.request_json(
                            "PUT",
                            f"/orgs/{tgt_org_q}/teams/{team_q}/memberships/{user_q}",
                            {"role": desired_role},
                        )
                        actions.append(TeamAction("update_role", display_login, role=desired_role))
                    except GitHubError as e:
                        errors.append(f"Failed to update role for {display_login}: {e.message}")
        elif login_lower in target_pending:
            # Query pending membership role via read-only GET
            pending_role = None
            try:
                _, membership_data, _ = target_client.request_json(
                    "GET",
                    f"/orgs/{tgt_org_q}/teams/{team_q}/memberships/{user_q}",
                )
                if isinstance(membership_data, dict):
                    pending_role = membership_data.get("role", "member")
                else:
                    pending_role = "member"
            except GitHubError as e:
                errors.append(f"Failed to inspect pending membership for {display_login}: {e.message}")

            if pending_role == desired_role:
                actions.append(TeamAction("skip_pending", display_login, role=desired_role))
            elif pending_role is not None:
                if dry_run:
                    actions.append(TeamAction("update_pending_role", display_login, role=desired_role))
                else:
                    try:
                        target_client.request_json(
                            "PUT",
                            f"/orgs/{tgt_org_q}/teams/{team_q}/memberships/{user_q}",
                            {"role": desired_role},
                        )
                        actions.append(TeamAction("update_pending_role", display_login, role=desired_role))
                    except GitHubError as e:
                        errors.append(f"Failed to update pending role for {display_login}: {e.message}")
        else:
            # Not in target active and not in target pending -> add member
            if dry_run:
                actions.append(TeamAction("add_member", display_login, role=desired_role))
            else:
                try:
                    target_client.request_json(
                        "PUT",
                        f"/orgs/{tgt_org_q}/teams/{team_q}/memberships/{user_q}",
                        {"role": desired_role},
                    )
                    actions.append(TeamAction("add_member", display_login, role=desired_role))
                except GitHubError as e:
                    errors.append(f"Failed to add member {display_login}: {e.message}")

    # 5. Process undesired active members (remove member)
    for login_lower, (active_display, active_role) in sorted(target_active.items()):
        if login_lower not in desired:
            active_q = urllib.parse.quote(active_display, safe="")
            if dry_run:
                actions.append(TeamAction("remove_member", active_display, role=active_role))
            else:
                try:
                    target_client.request_json(
                        "DELETE",
                        f"/orgs/{tgt_org_q}/teams/{team_q}/memberships/{active_q}",
                    )
                    actions.append(TeamAction("remove_member", active_display, role=active_role))
                except GitHubError as e:
                    errors.append(f"Failed to remove member {active_display}: {e.message}")

    # 6. Process undesired pending invitations (remove pending + cancel org invite if team_count <= 1)
    for login_lower, inv in sorted(target_pending.items()):
        if login_lower not in desired:
            pending_display = inv.get("login") or login_lower
            pending_q = urllib.parse.quote(pending_display, safe="")
            if dry_run:
                actions.append(TeamAction("remove_pending", pending_display))
            else:
                try:
                    target_client.request_json(
                        "DELETE",
                        f"/orgs/{tgt_org_q}/teams/{team_q}/memberships/{pending_q}",
                    )
                    actions.append(TeamAction("remove_pending", pending_display))
                except GitHubError as e:
                    errors.append(f"Failed to remove pending membership {pending_display}: {e.message}")

                team_count = inv.get("team_count", 0)
                inv_id = inv.get("id")
                if team_count <= 1 and inv_id is not None:
                    inv_id_q = urllib.parse.quote(str(inv_id), safe="")
                    try:
                        target_client.request_json(
                            "DELETE",
                            f"/orgs/{tgt_org_q}/invitations/{inv_id_q}",
                        )
                    except GitHubError as e:
                        errors.append(f"Failed to cancel org invitation {inv_id} for {pending_display}: {e.message}")

    return TeamSyncResult(
        team_slug=team_slug,
        created_team=created_team,
        actions=actions,
        errors=errors,
        desired_count=len(desired),
    )


def render_markdown_summary(results: list[TeamSyncResult], *, dry_run: bool) -> str:
    """Render a GitHub Flavored Markdown reconciliation report."""
    lines: list[str] = []
    title_suffix = " (DRY RUN)" if dry_run else ""
    lines.append(f"# Canonical -> Charmed Kubernetes Team Sync{title_suffix}")
    lines.append("")

    total_added = 0
    total_updated = 0
    total_removed = 0
    total_skipped = 0
    total_errors = 0

    for res in results:
        lines.append(f"## Team: `{res.team_slug}`")
        if res.created_team:
            status_text = "Would create team" if dry_run else "Created team"
            lines.append(f"- **Team status:** {status_text} `{res.team_slug}` (privacy: closed)")

        lines.append(f"- **Desired members in source:** {res.desired_count}")

        # Action breakdown
        added = [a for a in res.actions if a.action == "add_member"]
        updated = [a for a in res.actions if a.action in ("update_role", "update_pending_role")]
        removed = [a for a in res.actions if a.action in ("remove_member", "remove_pending")]
        skipped = [a for a in res.actions if a.action in ("skip_pending", "noop_org_owner", "noop")]

        total_added += len(added)
        total_updated += len(updated)
        total_removed += len(removed)
        total_skipped += len(skipped)
        total_errors += len(res.errors)

        lines.append(f"- **Changes:** {len(added)} added, {len(updated)} updated, {len(removed)} removed, {len(skipped)} skipped/noop")
        lines.append("")

        meaningful_actions = [a for a in res.actions if a.action != "noop"]
        if meaningful_actions:
            lines.append("| Username | Action | Role | Details |")
            lines.append("| --- | --- | --- | --- |")
            for a in meaningful_actions:
                role_str = a.role or "-"
                detail_str = a.detail or "-"
                lines.append(f"| `{a.username}` | `{a.action}` | {role_str} | {detail_str} |")
            lines.append("")
        else:
            lines.append("No changes required. Team is in sync.")
            lines.append("")

        if res.errors:
            lines.append("### Errors")
            for err in res.errors:
                lines.append(f"- :x: {err}")
            lines.append("")

    lines.append("### Summary")
    lines.append(f"- **Total Added:** {total_added}")
    lines.append(f"- **Total Updated:** {total_updated}")
    lines.append(f"- **Total Removed:** {total_removed}")
    lines.append(f"- **Total Skipped / No-op:** {total_skipped}")
    lines.append(f"- **Total Errors:** {total_errors}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for team sync script."""
    parser = argparse.ArgumentParser(
        description="Reconcile GitHub teams between canonical and charmed-kubernetes organizations.",
    )
    parser.add_argument(
        "--source-org",
        default="canonical",
        help="Source GitHub organization name (default: canonical)",
    )
    parser.add_argument(
        "--target-org",
        default="charmed-kubernetes",
        help="Target GitHub organization name (default: charmed-kubernetes)",
    )
    parser.add_argument(
        "--teams",
        nargs="+",
        default=["kubernetes", "kubernetes-admins"],
        help="Team slugs to reconcile (default: kubernetes kubernetes-admins)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and report reconciliation actions without executing mutations",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Allow empty source teams without tripping the circuit breaker",
    )

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    source_token = os.environ.get("CANONICAL_ORG_READ_TOKEN")
    target_token = os.environ.get("CK8S_ORG_SYNC_TOKEN")

    if not source_token or not target_token:
        sys.stderr.write(
            "Error: Both CANONICAL_ORG_READ_TOKEN and CK8S_ORG_SYNC_TOKEN environment variables must be set.\n"
        )
        return 2

    source_client = GitHubClient(token=source_token)
    target_client = GitHubClient(token=target_token)

    # Read target organization admins (owners) once
    target_org_owners: set[str] = set()
    try:
        tgt_org_q = urllib.parse.quote(args.target_org, safe="")
        owners_data = target_client.paginate(f"/orgs/{tgt_org_q}/members?role=admin&per_page=100")
        target_org_owners = {m["login"].lower() for m in owners_data if "login" in m}
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to fetch target org owners: {e}\n")

    results: list[TeamSyncResult] = []
    has_errors = False

    for team_slug in args.teams:
        try:
            result = reconcile_team(
                source_client=source_client,
                target_client=target_client,
                source_org=args.source_org,
                target_org=args.target_org,
                team_slug=team_slug,
                target_org_owners=target_org_owners,
                dry_run=args.dry_run,
                allow_empty=args.allow_empty,
            )
            if result.errors:
                has_errors = True
            results.append(result)
        except Exception as e:
            has_errors = True
            results.append(
                TeamSyncResult(
                    team_slug=team_slug,
                    created_team=False,
                    actions=[],
                    errors=[str(e)],
                    desired_count=0,
                )
            )

    summary = render_markdown_summary(results, dry_run=args.dry_run)
    print(summary)

    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary_path:
        try:
            with open(step_summary_path, "a", encoding="utf-8") as f:
                f.write(summary + "\n")
        except Exception as e:
            sys.stderr.write(f"Warning: Failed to write to GITHUB_STEP_SUMMARY: {e}\n")

    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
