# search-x-posts

An agent skill that searches X (Twitter) through the Grok Build CLI on your machine, then checks every post against X's public embed data. Posts that don't exist on X are dropped. The rest are rebuilt from X's copy, not Grok's.

It needs the [Grok Build CLI](https://x.ai/build), signed in to your xAI account. Grok runs the search in a locked-down, headless session, and the skill hands back structured JSON: author, time, text, likes, images and video, the quoted post, and the post being replied to.

## What it does

- **Searches X with Grok.** Your agent writes a focused brief. Grok runs its X search tools under a search cap and returns matching posts.
- **Checks every post.** Each post is looked up by its ID. Posts that don't exist are moved to `rejected`. The rest take their text, time and author from X.
- **Looks up known posts for free.** `--post <url>` skips Grok entirely.
- **Pulls media.** `--download-media <dir>` saves photos and video poster frames, so an agent that reads images can see them.
- **Cost.** 4 to 12 cents per capped search in testing, 27 cents at most.

## Requirements

- [Grok Build CLI](https://x.ai/build), signed in (`grok login`). Searches count toward your Grok plan's usage limits.
- Python 3.8 or later. Standard library only, nothing to install.
- Tested on macOS with Grok Build 1.0.41. Linux and Windows are untested.

## Install

With the [skills CLI](https://github.com/vercel-labs/skills), for Claude Code, Codex, Cursor and other agents:

```bash
npx skills add exgota/search-x-posts -g
```

As a Claude Code plugin:

```bash
/plugin marketplace add exgota/search-x-posts
/plugin install search-x-posts@search-x-posts
```

By hand: copy `skills/search-x-posts/` into `~/.claude/skills/` (or your agent's skills folder).

## Use

Ask your agent in plain words: "check X for reactions to the GPT-6 launch", "what did @AnthropicAI post this week", "pull the image from this post". Or run the script yourself:

```bash
cd skills/search-x-posts
python3 scripts/search_x_posts.py "The 5 most-liked posts from the last 48 hours about the GPT-6 launch."
python3 scripts/search_x_posts.py --post https://x.com/user/status/1234567890 --download-media ./media
python3 scripts/search_x_posts.py --help
```

A post in the output, trimmed to the main fields:

```json
{
  "id": "1234567890",
  "url": "https://x.com/user/status/1234567890",
  "handle": "user",
  "posted_at": "2026-09-24T18:59:24.000Z",
  "text": "the post's text",
  "likes": 42,
  "media": [{"type": "photo", "url": "https://pbs.twimg.com/media/example.jpg?name=large", "file": "/path/to/media/1234567890_1.jpg"}],
  "quoted_post": null,
  "in_reply_to": {"id": "1234567000", "handle": "other", "url": "https://x.com/other/status/1234567000", "text": "the parent post"},
  "verification": "verified"
}
```

`verification` is `verified`, `text_mismatch` (the post is real but Grok's text differed; X's text is shown), `unavailable` (deleted, protected or age-restricted) or `unchecked` (X's embed endpoint was unreachable). `SKILL.md` explains the fields the agent acts on.

## Updating

Once a day, the script checks this repository for a newer version. When one exists, its output includes `update_available`, and the skill tells your agent to ask you before updating. To update:

- Claude Code plugin: `/plugin marketplace update search-x-posts`, then `/plugin update search-x-posts@search-x-posts`
- Skills CLI: `npx skills update search-x-posts`
- By hand: copy the new `skills/search-x-posts/` over the old one.

Set `SEARCH_X_POSTS_NO_UPDATE_CHECK=1` to turn the check off.

## What leaves your machine

- Your brief goes to xAI, through Grok.
- The ID of every post Grok returns goes to X's embed endpoint. Media downloads come from X's media servers.
- Once a day, a request for this repository's `SKILL.md` goes to GitHub.

The script sends nothing else. It stores only media you ask to download, `--stream-log` output if you set it, and an update-check file in `~/.cache/search-x-posts/` (or under `$XDG_CACHE_HOME`). What the Grok CLI itself sends and stores is up to Grok.

## How it works

1. The script starts `grok -p` in a throwaway directory with isolation settings. Local tools are removed by name. Shell and file access are denied by permission rule. The process runs under Grok's strict kernel sandbox. Grok's loading of your Claude and Cursor configuration is switched off.
2. Grok searches X under a cap of 6 searches by default, 14 at most. The script counts searches in Grok's event stream and stops Grok if it goes more than 4 past the cap.
3. Grok answers in a fixed JSON schema. If Grok answered without running a single X search, the answer is discarded and the search retried once. This happens when Grok's X tools are missing from a session.
4. Every post is looked up on `cdn.syndication.twimg.com`, the endpoint behind X's embedded posts, and rebuilt from X's data.

## Testing

On Sep 24, 2026, a comparison of Grok's reasoning effort levels ran 24 searches over 4 tasks, with every returned post checked against X. The design was fixed before the runs. A run's score is the share of the best-known answer it returned.

| Effort | Mean score | Cost per call | Seconds per call |
| --- | --- | --- | --- |
| Low | 0.53 | $0.055 | 17 |
| Medium (default) | 0.67 | $0.110 | 52 |
| High | 0.50 | $0.040 | 35 |

Medium was the only level that used its search budget on a filtered query. When Grok's X tools worked, all 63 returned posts were real and their text matched. Invented posts came only from the 4 sessions where the X tools were missing, which is why the script now retries those. With 2 runs per task and level, only large differences are meaningful.

## Limitations

- **Unofficial endpoint.** `cdn.syndication.twimg.com` is undocumented and can change, rate-limit or disappear. If it becomes unreachable, posts come back marked `unchecked`, and the search itself still works.
- **Long posts.** The embed data carries only the first 280 characters. In a search, the full text comes from Grok, with its opening checked against X. A lookup with `--post` returns the first 280 characters, and `note` says so.
- **Renamed accounts.** Searching an old handle returns posts only up to the rename. Posts come back flagged `handle_changed_from`, and searching the current handle works.
- **Video.** Poster frames are downloaded. Video files are not; the MP4 URL is in the output.
- **Grok CLI changes.** The isolation flags and tool names match Grok Build 1.0.41. A later CLI version can rename them.

## Disclaimer

Not affiliated with xAI or X. This skill reads public posts through Grok and through X's public embed endpoint. Automated access to X is subject to X's terms of service; use it accordingly.

## License

MIT. See [LICENSE](LICENSE).
