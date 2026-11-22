# Design language — 青花 · Cobalt & Porcelain

**Motto: 众手成器** — *many hands make the vessel.*

A Studio task is a piece of craft. A lead throws the form, members each add a part,
reviewers check it, and a finished file comes out of the kiln. The interface borrows
its look from blue-and-white porcelain (青花瓷), where that kind of shared, patient work
has been done for centuries. The look is chosen to fit what the app does, not added on top.

## Principles

1. **Leave room (留白).** The porcelain ground is mostly empty. Agent messages carry no box,
   only a seal and text. Boxes are kept for objects you can act on: a delegation, a
   report, a question, a delivery.
2. **One structural colour.** Cobalt carries every piece of structure: the primary action,
   the selection, links, the brand seal. If something is cobalt, it is either clickable
   or it is the current place.
3. **Gold is for people.** Gold appears only where the human comes in or the work is
   handed over: your own seal (你), questions that wait for you, the 交付 tag on a final
   delivery, the gold ◆ on a task divider, and the answer box. Gold never decorates.
4. **Names are seals (印).** Every member is a square ink seal with one or two characters,
   coloured from one cobalt / indigo / slate / celadon family, so a crowded room still
   looks like one set. Small seals carry one character, like a real name chop.
   Seals sit side by side and never overlap.
5. **A conversation reads like a scroll.** The timeline is one centred column (820 px) with
   generous leading. Dividers are hairlines with a gold ◆. Activity folds into a single
   italic line instead of a stream of notifications.
6. **Slow, quiet motion.** New items appear with a short ink-bleed (opacity + 1.5 px blur).
   A running status breathes. Nothing bounces or slides across the screen.
   `prefers-reduced-motion` turns all of it off.

## Palette

| Token | Value | Role |
|---|---|---|
| `--porcelain` | `#f4f2ec` | page ground (warm, slightly grey white) |
| `--paper` / `--paper-2` | `#fbfaf6` / `#f8f6f0` | cards, sheets, the sidebar |
| `--ink` / `--ink-2` | `#1a2140` / `#3f4664` | text: a blue-black, never pure black |
| `--muted` / `--faint` | `#6e7390` / `#a2a5b6` | metadata, placeholders |
| `--line` | `#e3ded2` | hairlines (warm, so they sit on porcelain) |
| `--cobalt` / `--cobalt-deep` / `--cobalt-mid` | `#22409c` / `#162b72` / `#4b64b6` | structure, actions, selection |
| `--cobalt-wash` / `--cobalt-mist` | `#e7ebf6` / `#f0f2f9` | your messages, hover, selected rows |
| `--gold` / `--gold-deep` | `#c39629` / `#85640f` | human moments and delivery |
| `--gold-wash` / `--gold-mist` | `#f6edd3` / `#fbf6e8` | question and waiting backgrounds |
| `--celadon` | `#4e8a80` | success (completed, ok), a glaze green, not a traffic-light green |
| `--vermilion` | `#b0473a` | failure: seal-paste red, used sparingly |

Blue and gold are the two hues. Celadon and vermilion are the two glazes used for status.
Older rules still read the legacy names (`--primary`, `--panel`, `--bg`, …), which now
alias the tokens above.

## Type

- **Serif (宋体: Songti SC / Noto Serif SC)** for names and headings: room names, member
  names, view titles, ledger numerals, dialog titles. Serif means a person or a work.
- **Sans (PingFang SC / system)** for running text and controls.
- **Mono (SF Mono)** for paths, ids, times, counts and file types. Mono means it came from a machine.

## Motifs

- **Brand seal**: a rounded cobalt seal holding a vessel's mouth with two wave lines and
  a gold dot (the kiln's spark). Inline SVG in `index.html`. The assistant's welcome
  reuses it.
- **海水纹 waves**: a seigaiha arc pattern (`--waves`, an SVG data URI) used only as a thin
  band across the top of the welcome panel and along the bottom of the model-connection card.
- **Ledger**: workbench counts sit in one hairline box with serif numerals. The selected
  filter gets a coloured underline that draws in from the left.
- **File tiles**: every file is a tile with a solid mono type stamp. md/txt are cobalt-mid,
  html/css gold, py/js/ts celadon, json/csv slate.
- **Delivery mount**: the final answer is mounted like a painting, with a double inset
  border and a gold 交付 tag.

## Layout rules

- Timeline content, the live status, activity and the composer share one centred column
  (`max-width: 820px`). Your messages sit at that column's right edge.
- The room header keeps to two lines: the name, then seals · workspace · session.
  In a narrow room (`@container (max-width: 860px)`), header actions drop to icons. Their
  labels stay in the DOM for tooltips and screen readers.
- On phones (≤ 760 px) the sidebar becomes a horizontal strip, the ledger becomes 2×2,
  and the send button stays at the right edge of the composer bar.

## Files

- `miniagent/web/app.css`: tokens, seals, status chips, timeline, messages, composer, dialogs.
- `miniagent/web/studio.css`: sidebar, brand, connection card, welcome, directory views, member panel, room header.
- `miniagent/web/workbench.css`: ledger, task inspector, tool trace, file tiles, reader.
- `miniagent/web/app.js`: `AVATAR_COLORS` (seal inks), `avatar()` (one-character small seals), `brandSeal()`.

The static server loads assets into memory at start, so restart `miniagent studio` to see CSS changes.
