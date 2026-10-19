---
name: search-x-posts
description: Search X (Twitter) for live posts through the local Grok CLI, then check every post against X's own data. Use for breaking news, AI news and model launches, what people are saying about something, a named account's recent posts, a post or thread by URL, images or video in a post, topics from the last two weeks during research, and whenever the user says to use X, check Twitter, or search X.
license: MIT
compatibility: Requires the Grok Build CLI (xAI), signed in, and Python 3.8 or later. Tested on macOS with Grok Build 1.0.62.
metadata:
  version: "1.2.1"
---

# Search X posts

`scripts/search_x_posts.py` runs Grok headless with its X search tools and web search, and no shell or file access. It then looks up every post Grok returns in X's public embed data and rebuilds it from there: real author, time, text, likes, media, quoted post and reply parent. Run `python3 scripts/search_x_posts.py --help` for flags.

X posts are **leads**: what an account said, not what is true.

## Steps

1. **Brief.** Write one focused brief: who or what, the time window, what counts as a match, how many posts. Example: `The 5 most-liked posts from the last 48 hours about the GPT-6 launch.` When the user already gave post URLs, skip to lookup.
2. **Search or look up.**
   - Search: `python3 scripts/search_x_posts.py "<brief>"`. Defaults: 6 searches, 10 posts. Grok spends whatever search cap it gets, so size both to the brief (from an Oct 2026 test):

     | Brief | Flags | Why |
     |---|---|---|
     | One known post or thread | `--searches 2` | Found in 1 search; a cap of 14 spent 8 for the same answer. |
     | Launch or news sweep | `--posts 25` | Two 15-post runs overlapped on only 6 posts; the post cap limits breadth, not searches. |
     | What practitioners say (how-tos, experiences) | `--searches 14 --expand` | 14 searches found twice the relevant posts of 6; the substance sits in X Articles. |
     | One account's posts over a month | `--searches 14 --posts 40` | 40 posts cost 1.5 cents each and covered 21 of 30 days. |

     Run the script at most 2 times per research task unless the user asks for more, and let each run finish before starting the next. A corpus spanning several months needs one run per month: say so and get the user's go-ahead first.
   - Depth: `--expand` has Grok read linked X Articles and threads into `expanded_text`. X's embed data cannot check that text, so treat it as Grok's copy.
   - Lookup: `python3 scripts/search_x_posts.py --post <url or id>` (repeatable). It calls no Grok and costs nothing.
   - Images: add `--download-media <directory>`, then open each media item's `file` to see it. Videos come back as a poster frame plus an MP4 `url`.
3. **Read.** Each post carries a `verification` value:
   - `verified`: the post exists and Grok's text matches it.
   - `text_mismatch`: the post exists, but Grok's text differed. The text shown is X's; Grok's is in `grok_text`.
   - `unavailable`: X withholds it (deleted, protected or age-restricted). The fields are Grok's copy. In lookup mode, such a post goes to `rejected` instead.
   - `unchecked`: X's embed endpoint was unreachable. The fields are Grok's copy.

   `rejected` lists posts the script dropped, each with a `reason`: no status ID in the URL, no such post on X, an ID that belongs to a different post, or the same text as another account's post. `handle_changed_from` means the account was renamed: search again with the current `handle`. If `posts` is empty or the run stopped, narrow the brief and search once more, then report the gap.
4. **Check.** Confirm every claim the answer rests on against a primary source (the company's own site or docs, a filing, established reporting) with web search. A claim with no primary source stays attributed: "@handle says...".
5. **Report.** What the posts say, each linked, how sure you are, and what the posts leave unestablished. Mention the cost when a call passed 25 cents.

Done when every claim in the answer is either confirmed by a primary source or attributed to the post that makes it.

## Known behavior

- Long posts: X's embed data carries only the first 280 characters. In a search, when Grok's opening matches, the script keeps Grok's full text and says so in `note`. A lookup returns the first 280 characters, and `note` says so.
- Renamed accounts: searching an old handle returns posts only up to the rename. The posts come back with `handle_changed_from` set, and searching the current handle returns the recent ones.
- Signed out, Grok has no X tools. The script checks first and exits with "Grok is signed out": tell the user to run `grok login`.
- Grok sometimes answers without searching X. The script discards any answer with no X search behind it and retries once. `stopped: "X tools unavailable"` means both tries failed: report that X was unreachable.
- Grok plans have usage limits. The search cap exists because one uncapped question ran 45 searches and cost 1.84 USD.
