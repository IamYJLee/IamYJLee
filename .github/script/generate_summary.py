import json
import os
import re
import urllib.request
import urllib.error

README_PATH = "README.md"

SUMMARY_START = "<!--START_SECTION:activity_summary-->"
SUMMARY_END = "<!--END_SECTION:activity_summary-->"

MODELS_URL = "https://models.github.ai/inference/chat/completions"
MODEL = "openai/gpt-4o-mini"

OWNER = os.environ["GITHUB_REPOSITORY_OWNER"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]

ALLOWED_EVENT_TYPES = {
    "IssuesEvent",
    "IssueCommentEvent",
    "PullRequestEvent",
    "PullRequestReviewEvent",
    "ReleaseEvent",
}
MAX_EVENTS = 10


def gh_get(url: str):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "generate-summary-script",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def gh_get_safe(url: str):
    try:
        return gh_get(url)
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def replace_section(text: str, start: str, end: str, new_body: str) -> str:
    pattern = re.compile(rf"{re.escape(start)}.*?{re.escape(end)}", re.DOTALL)
    replacement = f"{start}\n{new_body}\n{end}"
    if not pattern.search(text):
        raise RuntimeError(f"Section markers not found: {start} ... {end}")
    return pattern.sub(replacement, text)


def fetch_recent_events():
    # Pull more than 10, then filter to the same event families we care about.
    url = f"https://api.github.com/users/{OWNER}/events/public?per_page=100"
    events = gh_get(url)
    filtered = [e for e in events if e.get("type") in ALLOWED_EVENT_TYPES]
    return filtered[:MAX_EVENTS]


def enrich_issue(issue_url: str):
    issue = gh_get_safe(issue_url)
    if not issue:
        return None

    labels = [x.get("name", "") for x in issue.get("labels", [])[:5]]
    comments_preview = []

    comments_url = issue.get("comments_url")
    if comments_url and issue.get("comments", 0) > 0:
        comments = gh_get_safe(comments_url)
        if isinstance(comments, list):
            for c in comments[:2]:
                comments_preview.append({
                    "user": c.get("user", {}).get("login", ""),
                    "body": truncate(c.get("body", ""), 240),
                })

    return {
        "number": issue.get("number"),
        "title": issue.get("title", ""),
        "state": issue.get("state", ""),
        "body": truncate(issue.get("body", ""), 700),
        "labels": labels,
        "html_url": issue.get("html_url", ""),
        "comments_preview": comments_preview,
    }


def enrich_pr(pr_url: str):
    pr = gh_get_safe(pr_url)
    if not pr:
        return None

    commits_preview = []
    commits_url = pr.get("commits_url")
    if commits_url:
        commits = gh_get_safe(commits_url)
        if isinstance(commits, list):
            for c in commits[:3]:
                commits_preview.append(
                    truncate(c.get("commit", {}).get("message", ""), 180)
                )

    review_comments_preview = []
    review_comments_url = pr.get("review_comments_url", "").replace("{/number}", "")
    if review_comments_url:
        review_comments = gh_get_safe(review_comments_url)
        if isinstance(review_comments, list):
            for rc in review_comments[:2]:
                review_comments_preview.append({
                    "user": rc.get("user", {}).get("login", ""),
                    "path": rc.get("path", ""),
                    "body": truncate(rc.get("body", ""), 240),
                })

    return {
        "number": pr.get("number"),
        "title": pr.get("title", ""),
        "state": pr.get("state", ""),
        "merged": bool(pr.get("merged")),
        "body": truncate(pr.get("body", ""), 800),
        "html_url": pr.get("html_url", ""),
        "changed_files": pr.get("changed_files", 0),
        "additions": pr.get("additions", 0),
        "deletions": pr.get("deletions", 0),
        "commits_preview": commits_preview,
        "review_comments_preview": review_comments_preview,
    }


def build_enriched_event(event: dict):
    event_type = event.get("type", "")
    repo = event.get("repo", {}).get("name", "")
    created_at = event.get("created_at", "")
    payload = event.get("payload", {})
    action = payload.get("action", "")

    enriched = {
        "event_type": event_type,
        "repo": repo,
        "created_at": created_at,
        "action": action,
    }

    if event_type == "PullRequestEvent":
        pr_url = payload.get("pull_request", {}).get("url")
        enriched["pull_request"] = enrich_pr(pr_url) if pr_url else None

    elif event_type == "PullRequestReviewEvent":
        pr_url = payload.get("pull_request", {}).get("url")
        review = payload.get("review", {}) or {}
        enriched["pull_request"] = enrich_pr(pr_url) if pr_url else None
        enriched["review"] = {
            "state": review.get("state", ""),
            "body": truncate(review.get("body", ""), 320),
        }

    elif event_type == "IssuesEvent":
        issue_url = payload.get("issue", {}).get("url")
        enriched["issue"] = enrich_issue(issue_url) if issue_url else None

    elif event_type == "IssueCommentEvent":
        issue_url = payload.get("issue", {}).get("url")
        comment = payload.get("comment", {}) or {}
        enriched["issue"] = enrich_issue(issue_url) if issue_url else None
        enriched["comment"] = {
            "body": truncate(comment.get("body", ""), 320),
            "html_url": comment.get("html_url", ""),
        }

    elif event_type == "ReleaseEvent":
        release = payload.get("release", {}) or {}
        enriched["release"] = {
            "name": release.get("name", ""),
            "tag_name": release.get("tag_name", ""),
            "body": truncate(release.get("body", ""), 320),
            "html_url": release.get("html_url", ""),
        }

    return enriched


def fallback_summary(enriched_events):
    if not enriched_events:
        return "- No recent public activity found."

    bullets = []
    for e in enriched_events[:5]:
        et = e.get("event_type", "")
        repo = e.get("repo", "")

        if et == "PullRequestEvent" and e.get("pull_request"):
            pr = e["pull_request"]
            status = "Merged" if pr.get("merged") else "Opened or updated"
            bullets.append(f"- {status} PR #{pr['number']} in `{repo}`: {pr['title']}.")
        elif et == "IssuesEvent" and e.get("issue"):
            issue = e["issue"]
            bullets.append(f"- Opened or updated issue #{issue['number']} in `{repo}`: {issue['title']}.")
        elif et == "IssueCommentEvent" and e.get("issue"):
            issue = e["issue"]
            bullets.append(f"- Commented on issue #{issue['number']} in `{repo}`: {issue['title']}.")
        elif et == "PullRequestReviewEvent" and e.get("pull_request"):
            pr = e["pull_request"]
            bullets.append(f"- Reviewed PR #{pr['number']} in `{repo}`: {pr['title']}.")
        elif et == "ReleaseEvent" and e.get("release"):
            rel = e["release"]
            bullets.append(f"- Released `{rel['tag_name']}` in `{repo}`.")
        else:
            bullets.append(f"- Recorded recent `{et}` activity in `{repo}`.")

    return "\n".join(bullets[:5])


def call_github_models(enriched_events):
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You summarize a developer's recent GitHub activity for a profile README. "
                    "You are given structured event data enriched with pull request, issue, comment, review, and release details. "
                    "Write 4 to 6 markdown bullet points. "
                    "Be concrete and conservative. "
                    "For pull requests, explain what problem was addressed and how if the title/body/commit messages make it clear. "
                    "For issues, explain what problem was raised. "
                    "For comments and reviews, explain what topic was discussed. "
                    "For merged pull requests, explicitly mention that they were merged. "
                    "Group related items when helpful. "
                    "Do not invent details that are not supported by the provided data. "
                    "Do not add a heading."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Summarize the following enriched recent GitHub activity.\n\n"
                    "Requirements:\n"
                    "- 4 to 6 markdown bullets\n"
                    "- Focus on technical content and concrete work\n"
                    "- Mention repositories when useful\n"
                    "- Avoid vague phrases like 'likely addresses a bug' or 'active involvement'\n"
                    "- Only state details supported by the data below\n\n"
                    f"{json.dumps(enriched_events, ensure_ascii=False, indent=2)}"
                ),
            },
        ],
        "temperature": 0.2,
        "max_tokens": 350,
    }

    req = urllib.request.Request(
        MODELS_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )

    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    return data["choices"][0]["message"]["content"].strip()


def main():
    with open(README_PATH, "r", encoding="utf-8") as f:
        readme = f.read()

    recent_events = fetch_recent_events()
    enriched_events = [build_enriched_event(e) for e in recent_events]

    try:
        summary = call_github_models(enriched_events)
        if not summary.strip():
            summary = fallback_summary(enriched_events)
    except Exception:
        summary = fallback_summary(enriched_events)

    if not summary.startswith("- "):
        summary = "- " + summary.lstrip("- ").strip()

    updated = replace_section(readme, SUMMARY_START, SUMMARY_END, summary)

    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(updated)


if __name__ == "__main__":
    main()
