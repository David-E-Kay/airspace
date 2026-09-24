# Hover highlight for clickable sessions — design

Date: 2026-09-24
Status: implemented on branch `hover-highlight`

## Goal

When the mouse is over a session that can be clicked through to its app, the
item should visibly lift out of the page, so it is obvious (a) which session
the click will open and (b) that clicking does something.

Applies to both places a session appears:

1. The "stopped for you" strip at the top (`.triage li`).
2. The session cards below (`.card`).

## What already exists

- `dashboard.py:1300` — `.card.clickable { cursor:pointer; }`
- `dashboard.py:1321` — `.triage li.jump { cursor:pointer; }`
- `dashboard.py:1427`, `dashboard.py:1448` — the `clickable` / `jump` class is
  added only when the row has a `deep_link`. Rows without one (e.g. a session
  in a bare terminal) are not clickable.

So the pointer cursor is already done. The gap is that nothing about the item
itself changes on hover.

## Design

CSS only. No JavaScript, no markup change, no new classes. Hover styles hang
off the existing `clickable` and `jump` classes, so an item that cannot be
clicked never highlights — the highlight is an honest "this is clickable".

### Cards (`.card.clickable:hover`)

- Background lifts from `#1c1f25` to a lighter shade (about `#232730`).
- The top and bottom border (`#282c34`) brightens to about `#4a5260`, matching
  the existing `.btn:hover` border.
- The left (state) and right (app) coloured edges are **not** touched. They
  carry meaning and must keep their colours.
- No `box-shadow`: the `pulse` animation already uses `box-shadow`, and a hover
  shadow would fight it on pulsing cards.
- No movement or resizing (no `transform`, no `border-width` change), so the
  cards around it do not shift.
- A short fade (`transition: background-color .12s, border-color .12s`) so the
  change reads as a highlight rather than a flicker.

### Strip rows (`.triage li.jump:hover`)

- A faint amber background, in the strip's own colour family (about
  `#33280f`), with a small `border-radius` and a few px of side padding so the
  highlight has a shape. The padding is applied to all `.triage li` rows (not
  only on hover) so text does not jump sideways when the mouse arrives. The
  list is pulled out by the same amount on both sides, so the text stays in
  line with the heading and the highlight is even.
- No underline. An underline set on the row spreads to every piece inside it,
  and the label and app name cannot opt back out, so the whole row would
  underline. Underlining only the title would need a markup change; the
  background band and pointer cursor are signal enough.

### Light mode

Light mode is the whole page colour-inverted (`html.light` filter,
`dashboard.py:1369`). The hover colours invert with everything else, so no
separate light-mode values are needed. Check it by eye.

## Out of scope

- **Keyboard access** (Tab to a card, Enter to open). The cards are plain
  `<div>`s with an `onclick`, so they cannot be reached with the keyboard
  today. Fixing that means markup changes (`tabindex`, `role="link"`, a key
  handler) and is a separate change. Worth doing later.
- Tooltips ("Open in Claude"/"Open in Codex"). Not asked for.
- Any change to what a click does.

## Risks

- **Auto-refresh.** Every few seconds the page swaps in a fresh `<body>`
  (`dashboard.py:1386`). The element under the mouse is replaced, and the
  browser may drop the hover highlight until the mouse moves. Chromium-based
  browsers normally re-check hover after the page changes, so this is
  expected to be fine — verify it, and accept a brief flicker if not.

## Testing

- Extend `test_card_jumps_to_the_session_only_when_a_deep_link_is_known`
  (`tests/test_dashboard.py:206`) to assert the page CSS contains
  `.card.clickable:hover` and `.triage li.jump:hover`. Prove it can fail by
  running it before the CSS is added.
- By eye, in the running board: hover a linked card, an unlinked card, a
  pulsing card, and a strip row; repeat in light mode; hold the mouse still
  across one auto-refresh.
