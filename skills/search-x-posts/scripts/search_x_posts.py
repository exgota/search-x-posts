#!/usr/bin/env python3
"""Search X through the Grok Build CLI, check every post against X's public embed data, and print JSON.

Two modes:
  search   search_x_posts.py "<brief>"             Grok searches X; the script checks what it returns.
  lookup   search_x_posts.py --post <url or id>    No Grok call: fetch known posts directly (free, instant).

Grok runs headless with its server-side X search tools and nothing that touches this machine: local
tools are removed by name (--disallowed-tools), shell and file access are denied by permission rule
(--deny), and the process runs under the kernel sandbox (--sandbox strict) in a throwaway directory.
Removing tools by name matters: an allowlist (--tools) drops the X tools unless it names x_search.

Grok runs as this skill's own agent (x_search_agent.md, --agent), never the user's default agent: a
default agent set in ~/.grok/config.toml once ran every search under an unrelated persona and model.
The agent's short system prompt replaces Grok's coding prompt (437 bytes against about 7,400).

X tool results never appear in Grok's output stream, so posts come from Grok's final answer,
constrained by a JSON schema. Every post is then looked up on cdn.syndication.twimg.com (the
unofficial endpoint behind X's embedded posts) and rebuilt from that data: real author, time, text,
likes, media, quoted post and reply parent. A post that does not exist is moved to "rejected".
Long posts are the exception: the endpoint carries only their first 280 characters, so Grok's full
text is kept when its opening matches.

Guards, each from a failure seen in testing (Sep 2026):
- Search cap in the prompt, plus a hard stop at cap + SEARCH_CEILING_MARGIN (one uncapped call ran 45).
- Grok must be signed in, checked with `grok models` before any search (under a second, no model
  call). Signed out, Grok has no X tools and retries its 401 for minutes (Oct 2026).
- An answer with no X search behind it is discarded and retried once (the X tools were missing from
  4 of 24 sessions, and Grok then invented posts).
- A post whose text matches an earlier post by another account is moved to "rejected" (copied posts
  passed as separate voices in an Oct 2026 test).
- Embed lookups retry with backoff (2 s, then 6 s): four parallel runs once left a whole run unchecked.
- Effort defaults to medium: in a 24-run comparison it was the only level that used its search budget
  on a filtered query (5 of 6 qualifying posts, against 1 for low and high).

Output (stdout): {"posts": [...], "rejected": [...], "x_searches", "web_searches", "search_queries",
"stopped", "attempts", "seconds", "tokens", "cost_usd", "effort", "model"}
"""
import argparse
import contextlib
import datetime
import difflib
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

DEFAULT_SEARCHES = 6
MAXIMUM_SEARCHES = 14
SEARCH_CEILING_MARGIN = 4
DEFAULT_POSTS = 10
DEFAULT_EFFORT = "medium"
ATTEMPTS_WHEN_TOOLS_MISSING = 2
DEFAULT_TIMEOUT_SECONDS = 300

EMBED_ENDPOINT = "https://cdn.syndication.twimg.com/tweet-result"
REQUEST_HEADERS = {"User-Agent": "search-x-posts/1.0"}
EMBED_TIMEOUT_SECONDS = 15
EMBED_PAUSE_SECONDS = 0.2
EMBED_FAILURES_BEFORE_GIVING_UP = 3
EMBED_ATTEMPTS = 3
COPIED_TEXT_MINIMUM_LENGTH = 80
TEXT_MATCH_THRESHOLD = 0.85

AGENT_DEFINITION = os.path.join(os.path.dirname(os.path.abspath(__file__)), "x_search_agent.md")

DISALLOWED_TOOLS = [
    "run_terminal_command", "run_terminal_cmd", "write", "search_replace", "read_file",
    "list_dir", "grep", "image_gen", "image_edit", "image_to_video", "reference_to_video",
    "spawn_subagent", "workflow", "scheduler_create", "scheduler_delete", "scheduler_list",
    "monitor", "web_fetch", "use_tool", "search_tool",
]
DENY_RULES = ["Bash", "Read", "Write", "Edit"]

# Grok otherwise loads the user's Claude and Cursor setup (instructions, skills, MCP servers, hooks).
ISOLATION_ENVIRONMENT = {
    f"GROK_{vendor}_{kind}_ENABLED": "false"
    for vendor in ("CLAUDE", "CURSOR")
    for kind in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS")
}

POST_SCHEMA = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "posted_at": {"type": "string"},
                    "url": {"type": "string"},
                    "text": {"type": "string"},
                    "likes": {"type": "integer"},
                    "expanded_text": {"type": "string"},
                },
                "required": ["handle", "posted_at", "url", "text"],
            },
        }
    },
    "required": ["posts"],
}

PROMPT_TEMPLATE = """You are searching X for another agent. Your X tools are x_keyword_search, x_semantic_search, x_user_search and x_thread_fetch. The current time is {now}.

Brief: {brief}

Rules:
- Make at most {searches} X tool calls in total. Plan them before the first call, and stop when the brief is answered.
- Use the shortest time window that answers the brief, freshest first.
- Use x_keyword_search in Top mode for high-engagement posts and Latest mode for new posts, and x_semantic_search for other wordings. Use since:, until: and from: operators when the brief sets a window or an account.
- Use web_search at most once, and only to identify who or what the brief is about.
- Return at most {posts} posts, each one a post your X tools returned in this session. Copy its full text exactly. Give its URL as https://x.com/<handle>/status/<id> and its like count when the tool shows it.
- Skip link lists, listicles and text copied from another account, unless the brief asks for them.
- If nothing matches, return an empty posts list."""

EXPAND_RULE = """
- For each post you return that links an X Article or opens a thread, fetch it with x_thread_fetch and put the article's or thread's full text in expanded_text. These fetches count toward your X tool calls."""


class ScriptError(Exception):
    pass


# Grok

def find_grok_binary():
    candidates = [os.environ.get("GROK_BINARY"), os.path.expanduser("~/.grok/bin/grok"), shutil.which("grok")]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise ScriptError("Grok Build CLI not found. Install it (https://x.ai/build), run `grok login`, "
                      "or point GROK_BINARY at the binary.")


def require_sign_in(binary):
    try:
        check = subprocess.run([binary, "models"], capture_output=True, text=True, timeout=30,
                               env={**os.environ, **ISOLATION_ENVIRONMENT})
    except (OSError, subprocess.TimeoutExpired) as problem:
        raise ScriptError(f"Could not check Grok's sign-in: {problem}")
    if "not authenticated" in (check.stdout + check.stderr).lower():
        raise ScriptError("Grok is signed out, so its X tools are unavailable. Run `grok login`, then search again.")


def build_command(binary, arguments, workspace):
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prompt = PROMPT_TEMPLATE.format(now=now, brief=arguments.brief, searches=arguments.searches, posts=arguments.posts)
    if arguments.expand:
        prompt += EXPAND_RULE
    command = [
        binary, "-p", prompt,
        "--cwd", workspace,
        "--agent", AGENT_DEFINITION,
        "--no-memory", "--no-subagents", "--max-turns", "4",
        "--sandbox", "strict",
        "--disallowed-tools", ",".join(DISALLOWED_TOOLS),
        "--json-schema", json.dumps(POST_SCHEMA),
        "--output-format", "streaming-json",
    ]
    for rule in DENY_RULES:
        command += ["--deny", rule]
    if arguments.effort:
        command += ["--reasoning-effort", arguments.effort]
    if arguments.model:
        command += ["--model", arguments.model]
    return command


def last_posts_object(text):
    """Grok can print draft answers before the final one. Keep the last object that has posts."""
    decoder = json.JSONDecoder()
    found = None
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            return found
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            index = start + 1
            continue
        if isinstance(value, dict) and isinstance(value.get("posts"), list):
            found = value
        index = end


def run_grok_once(binary, arguments):
    started = time.monotonic()
    result = {"posts": [], "x_searches": 0, "web_searches": 0, "search_queries": [], "stopped": None,
              "seconds": None, "tokens": None, "cost_usd": None}
    ceiling = arguments.searches + SEARCH_CEILING_MARGIN
    answer_text = []
    end_event = None
    unparsed_lines = []

    with tempfile.TemporaryDirectory(prefix="search_x_posts_") as workspace:
        error_log_path = os.path.join(workspace, "grok_stderr.log")
        with contextlib.ExitStack() as open_files:
            try:
                stream_log = open_files.enter_context(open(arguments.stream_log, "a")) if arguments.stream_log else None
                error_log = open_files.enter_context(open(error_log_path, "w"))
                process = subprocess.Popen(
                    build_command(binary, arguments, workspace),
                    stdout=subprocess.PIPE, stderr=error_log, text=True,
                    env={**os.environ, **ISOLATION_ENVIRONMENT},
                )
            except OSError as problem:
                return result, f"grok could not start: {problem}"

            def stop_for_timeout():
                result["stopped"] = "timeout"
                process.kill()

            timer = threading.Timer(arguments.timeout, stop_for_timeout)
            timer.start()
            try:
                for line in process.stdout:
                    if stream_log:
                        stream_log.write(line)
                    try:
                        event = json.loads(line)
                    except ValueError:
                        if line.strip():
                            unparsed_lines.append(line.strip())
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind = event.get("type")
                    if kind == "text":
                        answer_text.append(event.get("data", ""))
                    elif kind == "tool_call":
                        variant = (event.get("rawInput") or {}).get("variant")
                        if variant == "XSearch":
                            result["x_searches"] += 1
                        elif variant == "WebSearch":
                            result["web_searches"] += 1
                        if result["x_searches"] > ceiling:
                            result["stopped"] = "search ceiling"
                            process.kill()
                            break
                    elif kind == "tool_call_update":
                        raw_output = event.get("rawOutput") or {}
                        if raw_output.get("name"):
                            result["search_queries"].append({"tool": raw_output["name"], "input": raw_output.get("input")})
                        elif (raw_output.get("action") or {}).get("query"):
                            result["search_queries"].append({"tool": "web_search", "input": raw_output["action"]["query"]})
                    elif kind == "end":
                        end_event = event
            finally:
                timer.cancel()
                process.wait()
        with open(error_log_path) as error_log:
            error_output = error_log.read().strip()

    result["seconds"] = round(time.monotonic() - started, 1)
    if end_event:
        result["tokens"] = (end_event.get("usage") or {}).get("total_tokens")
        result["cost_usd"] = end_event.get("total_cost_usd")
    if result["stopped"]:
        return result, f"grok stopped: {result['stopped']}"
    answer = last_posts_object("".join(answer_text))
    if answer is None:
        detail = error_output or " | ".join(unparsed_lines) or "no posts object in grok's answer"
        return result, f"grok returned no usable answer: {detail[:500]}"
    result["posts"] = answer["posts"]
    return result, None


def run_grok(arguments):
    binary = find_grok_binary()
    require_sign_in(binary)
    spent ={"seconds": 0.0, "tokens": 0, "cost_usd": 0.0}
    for attempt in range(1, ATTEMPTS_WHEN_TOOLS_MISSING + 1):
        result, error = run_grok_once(binary, arguments)
        for key in spent:
            spent[key] += result[key] or 0
        result.update({key: round(value, 4) for key, value in spent.items()}, attempts=attempt)
        if error or result["x_searches"] > 0:
            return result, error
        result["posts"] = []
        result["stopped"] = "X tools unavailable"
    return result, "grok's X tools were unavailable on every attempt"


# X embed data

class EmbedClient:
    """Looks posts up on X's embed endpoint. After repeated failures it stops trying, so a broken
    endpoint costs seconds, not minutes, and posts come back marked "unchecked"."""

    def __init__(self):
        self.consecutive_failures = 0

    @staticmethod
    def token(status_id):
        # The token X's own embed script sends: (id / 1e15 * pi) in base 36, zeros and point removed.
        # The endpoint does not check it today; sending it keeps the request identical to X's.
        value = int(status_id) / 1e15 * math.pi
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        whole, fraction = int(value), value - int(value)
        whole_digits = ""
        while whole:
            whole_digits = digits[whole % 36] + whole_digits
            whole //= 36
        fraction_digits = ""
        for _ in range(12):
            fraction *= 36
            digit = int(fraction)
            fraction_digits += digits[digit]
            fraction -= digit
        return ((whole_digits or "0") + fraction_digits).replace("0", "")

    def look_up(self, status_id):
        """Return (status, data): status is found, not_found, unavailable (deleted, protected or
        age-restricted) or unchecked (the endpoint could not be reached)."""
        if self.consecutive_failures >= EMBED_FAILURES_BEFORE_GIVING_UP:
            return "unchecked", None
        url = f"{EMBED_ENDPOINT}?id={status_id}&token={self.token(status_id)}"
        for attempt in range(EMBED_ATTEMPTS):
            try:
                request = urllib.request.Request(url, headers=REQUEST_HEADERS)
                body = urllib.request.urlopen(request, timeout=EMBED_TIMEOUT_SECONDS).read()
                self.consecutive_failures = 0
                time.sleep(EMBED_PAUSE_SECONDS)
                data = json.loads(body) if body.strip() else {}
                kind = data.get("__typename")
                if kind == "Tweet":
                    return "found", data
                return ("unavailable", data) if kind == "TweetTombstone" or data else ("not_found", None)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    self.consecutive_failures = 0
                    return "not_found", None
            except (urllib.error.URLError, OSError, ValueError):
                pass
            if attempt + 1 < EMBED_ATTEMPTS:
                time.sleep(2 * 3 ** attempt)  # 2 s, then 6 s: parallel runs trip X's rate limit
        self.consecutive_failures += 1
        return "unchecked", None


STATUS_URL_PATTERN = re.compile(r"(?:twitter\.com|x\.com)/(?:#!/)?(?:[A-Za-z0-9_]+|i/web|i)/status(?:es)?/(\d+)", re.I)


def status_id_from(value):
    value = (value or "").strip()
    if value.isdigit():
        return value
    match = STATUS_URL_PATTERN.search(value)
    return match.group(1) if match else None


def readable_text(data):
    """Expand t.co links and drop the links X appends for attached media."""
    text = data.get("text") or ""
    entities = data.get("entities") or {}
    for link in entities.get("urls") or []:
        if link.get("url") and link.get("expanded_url"):
            text = text.replace(link["url"], link["expanded_url"])
    for media in (entities.get("media") or []) + (data.get("mediaDetails") or []):
        if media.get("url"):
            text = text.replace(media["url"], "")
    return html.unescape(text).strip()


def media_from(data):
    items = []
    for media in data.get("mediaDetails") or []:
        kind = media.get("type")
        size = media.get("original_info") or {}
        if kind == "photo":
            items.append({"type": "photo", "url": media["media_url_https"] + "?name=large",
                          "width": size.get("width"), "height": size.get("height")})
        elif kind in ("video", "animated_gif"):
            video_info = media.get("video_info") or {}
            variants = [v for v in video_info.get("variants") or [] if v.get("content_type") == "video/mp4"]
            best = max(variants, key=lambda v: v.get("bitrate") or 0) if variants else {}
            duration = video_info.get("duration_millis")
            items.append({"type": kind, "url": best.get("url"), "poster": media.get("media_url_https"),
                          "duration_seconds": round(duration / 1000) if duration else None,
                          "width": size.get("width"), "height": size.get("height")})
    return items


def is_available(data):
    return isinstance(data, dict) and data.get("__typename") != "TweetTombstone" and bool(data.get("id_str"))


def post_from_embed(data, include_context=True):
    user = data.get("user") or {}
    handle = user.get("screen_name")
    post = {
        "id": data.get("id_str"),
        "url": f"https://x.com/{handle or 'i/web'}/status/{data.get('id_str')}",
        "handle": handle,
        "name": user.get("name"),
        "posted_at": data.get("created_at"),
        "text": readable_text(data),
        "likes": data.get("favorite_count"),
        "replies": data.get("conversation_count", data.get("reply_count")),
        "media": media_from(data),
        "long_post": "note_tweet" in data,
    }
    if include_context:
        quoted = data.get("quoted_tweet")
        post["quoted_post"] = None
        if quoted:
            post["quoted_post"] = (post_from_embed(quoted, include_context=False) if is_available(quoted) else
                                   {"id": data.get("quoted_status_id_str"), "unavailable": True,
                                    "note": "X withholds the quoted post (deleted, protected or age-restricted)"})
        parent_id = data.get("in_reply_to_status_id_str")
        post["in_reply_to"] = None
        if parent_id:
            parent = data.get("parent") or {}
            parent_available = bool(parent) and is_available(parent)
            parent_handle = data.get("in_reply_to_screen_name") or (parent.get("user") or {}).get("screen_name")
            post["in_reply_to"] = {"id": parent_id, "handle": parent_handle,
                                   "url": f"https://x.com/{parent_handle or 'i/web'}/status/{parent_id}",
                                   "text": readable_text(parent) if parent_available else None}
            if parent and not parent_available:
                post["in_reply_to"]["unavailable"] = True
    return post


def comparable(text):
    text = html.unescape(text or "")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"^(?:@\w+\s+)+", "", text.strip())  # reply mentions: X stores them, Grok often drops them
    return re.sub(r"\s+", " ", text).strip().casefold()


def text_similarity(grok_text, real_text, real_is_truncated):
    grok_text, real_text = comparable(grok_text), comparable(real_text)
    if not grok_text or not real_text:
        return 0.0
    if real_is_truncated:
        grok_text = grok_text[:len(real_text)]
    return round(difflib.SequenceMatcher(None, grok_text, real_text).ratio(), 3)


def checked_post(grok_post, client):
    """Rebuild one of Grok's posts from X's own data. Returns (post, None) or (None, rejection)."""
    status_id = status_id_from(grok_post.get("url"))
    if not status_id:
        return None, {**grok_post, "reason": "no status id in the URL"}
    status, data = client.look_up(status_id)
    if status == "not_found":
        return None, {**grok_post, "reason": "post does not exist on X (likely invented)"}
    if status in ("unavailable", "unchecked"):
        note = ("X withholds this post (deleted, protected or age-restricted); this is Grok's copy" if status == "unavailable"
                else "X's embed endpoint could not be reached; this is Grok's copy")
        return {**grok_post, "id": status_id, "verification": status, "note": note}, None

    post = post_from_embed(data)
    if grok_post.get("expanded_text"):
        post["expanded_text"] = grok_post["expanded_text"]  # an X Article or thread Grok fetched; X's embed data cannot check it
    similarity =text_similarity(grok_post.get("text"), post["text"], post["long_post"])
    handle_matches = (grok_post.get("handle") or "").lstrip("@").casefold() == (post["handle"] or "").casefold()
    if similarity < TEXT_MATCH_THRESHOLD and not handle_matches:
        return None, {**grok_post, "reason": f"status id belongs to a different post, by @{post['handle']}"}
    if similarity < TEXT_MATCH_THRESHOLD:
        post.update(verification="text_mismatch", grok_text=grok_post.get("text"),
                    note="Grok's text differs from the post; the text shown is X's")
        return post, None
    post["verification"] = "verified"
    if not handle_matches and grok_post.get("handle"):
        post["handle_changed_from"] = grok_post["handle"].lstrip("@")  # the account was renamed; search its current handle
    if post["long_post"] and len(grok_post.get("text") or "") > len(post["text"]):
        post["text"] = grok_post["text"]  # X's embed data carries only the first 280 characters of a long post
        post["note"] = "long post: full text from Grok, its opening checked against X"
    return post, None


def looked_up_post(reference, client):
    status_id = status_id_from(reference)
    if not status_id:
        return None, {"reference": reference, "reason": "not a post URL or id"}
    status, data = client.look_up(status_id)
    if status == "found":
        post = post_from_embed(data)
        post["verification"] = "verified"
        if post["long_post"]:
            post["note"] = "long post: X's embed data carries only the first 280 characters"
        return post, None
    reasons = {"not_found": "post does not exist on X",
               "unavailable": "X withholds this post (deleted, protected or age-restricted)",
               "unchecked": "X's embed endpoint could not be reached"}
    return None, {"reference": reference, "id": status_id, "reason": reasons[status]}


def download_media(posts, directory):
    os.makedirs(directory, exist_ok=True)
    for post in posts:
        for owner in (post, post.get("quoted_post") or {}):
            for number, item in enumerate(owner.get("media") or [], 1):
                source = item["url"] if item["type"] == "photo" else item.get("poster")
                if not source:
                    continue
                suffix = "" if item["type"] == "photo" else "_poster"
                extension = os.path.splitext(source.split("?")[0])[1] or ".jpg"
                path = os.path.join(directory, f"{owner['id']}_{number}{suffix}{extension}")
                try:
                    request = urllib.request.Request(source, headers=REQUEST_HEADERS)
                    with urllib.request.urlopen(request, timeout=30) as response, open(path, "wb") as file:
                        file.write(response.read())
                    item["file"] = os.path.abspath(path)
                except (urllib.error.URLError, OSError) as error:
                    item["download_error"] = str(error)[:200]


def main():
    parser = argparse.ArgumentParser(description="Search X through the Grok Build CLI, check every post against X, and print JSON.")
    parser.add_argument("brief", nargs="?", help="what to find: topic or account, time window, and what counts as a match")
    parser.add_argument("--post", action="append", default=[], metavar="URL_OR_ID",
                        help="look up a known post directly, without Grok (repeatable)")
    parser.add_argument("--searches", type=int, default=DEFAULT_SEARCHES, help=f"X search cap (default {DEFAULT_SEARCHES}, maximum {MAXIMUM_SEARCHES})")
    parser.add_argument("--posts", type=int, default=DEFAULT_POSTS, help=f"most posts to return (default {DEFAULT_POSTS})")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh"], default=DEFAULT_EFFORT, help=f"Grok reasoning effort (default {DEFAULT_EFFORT})")
    parser.add_argument("--model", help="Grok model id (default: Grok's default model)")
    parser.add_argument("--download-media", metavar="DIRECTORY", help="save photos and video posters here; each media item gains a file path")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="seconds before Grok is stopped")
    parser.add_argument("--stream-log", help="append Grok's raw event stream to this file")
    parser.add_argument("--expand", action="store_true",
                        help="have Grok read linked X Articles and threads into expanded_text (uses X tool calls)")
    arguments = parser.parse_args()
    if not arguments.brief and not arguments.post:
        parser.error("give a brief to search, or --post to look up known posts")
    if not 1 <= arguments.searches <= MAXIMUM_SEARCHES:
        parser.error(f"--searches must be between 1 and {MAXIMUM_SEARCHES}")

    client = EmbedClient()
    posts, rejected, error = [], [], None
    result = {"x_searches": 0, "web_searches": 0, "search_queries": [], "stopped": None, "attempts": 0,
              "seconds": 0.0, "tokens": 0, "cost_usd": 0.0, "effort": None, "model": None}
    try:
        for reference in arguments.post:
            try:
                post, rejection = looked_up_post(reference, client)
            except Exception as problem:  # unexpected embed data: report it, keep the other lookups
                post, rejection = None, {"reference": reference, "reason": f"lookup failed: {problem}"}
            if post:
                posts.append(post)
            else:
                rejected.append(rejection)
        if arguments.brief:
            grok_result, error = run_grok(arguments)
            result.update({key: value for key, value in grok_result.items() if key != "posts"},
                          effort=arguments.effort, model=arguments.model)
            for grok_post in grok_result["posts"]:
                if not isinstance(grok_post, dict):
                    rejected.append({"entry": grok_post, "reason": "malformed entry in Grok's answer"})
                    continue
                try:
                    post, rejection = checked_post(grok_post, client)
                except Exception as problem:  # unexpected embed data: keep Grok's copy rather than lose the search
                    post, rejection = {**grok_post, "verification": "unchecked", "note": f"check failed: {problem}"}, None
                if post:
                    posts.append(post)
                else:
                    rejected.append(rejection)
    except ScriptError as problem:
        print(str(problem), file=sys.stderr)
        sys.exit(2)

    unique, seen, seen_texts = [], set(), {}
    for post in posts:
        if post.get("id") in seen:
            continue
        text_key = comparable(post.get("text"))
        original = seen_texts.get(text_key) if len(text_key) >= COPIED_TEXT_MINIMUM_LENGTH else None
        if original and original.get("handle") != post.get("handle"):
            rejected.append({**post, "reason": f"same text as {original.get('id')} by @{original.get('handle')}"})
            continue
        seen.add(post.get("id"))
        seen_texts.setdefault(text_key, post)
        unique.append(post)
    posts = unique if arguments.post and not arguments.brief else unique[:arguments.posts]
    if arguments.download_media:
        download_media(posts, arguments.download_media)

    output = {"posts": posts, "rejected": rejected, **result}
    print(json.dumps(output, indent=2, ensure_ascii=False))
    if error:
        print(error, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
