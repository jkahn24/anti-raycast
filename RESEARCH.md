# Launcher search UX/UI — verified best practices → Flint gap analysis

Deep-research run 2026-07-13 (104 agents, 22 sources, 25 claims verified 3-vote
adversarial, 24 confirmed / 1 refuted). Sources verified against LIVE code:
fzf `algo.go`, VS Code `fuzzyScorer.ts`, PowerToys `Result.cs`/`UserSelectedRecord.cs`,
Firefox urlbar ranking docs, Raycast 2.0 engineering deep-dive, Superhuman palette
article, Nielsen + Dan Luu latency evidence.

## Verified findings (all HIGH confidence, 3-0 votes)

### Ranking
1. **Boundary-aware scoring beats plain fuzzy.** fzf and VS Code independently
   converge: word-start/separator/camelCase hits score FAR above mid-word
   subsequence hits; the FIRST typed char on a boundary gets a doubled bonus
   (fzf: base 16, +10 whitespace-boundary, +9 delimiter, +7 camel, first-char ×2.
   VS Code: +8 word-start, +4-5 separator, +2 camel).
2. **Gap penalties + capped consecutive bonuses** (fzf: -3 gap start, -1 extend;
   VS Code: full bonus only first 3 consecutive) so acronym-quality matches can
   beat long sloppy runs.
3. **Structural tiers are unjumpable** (VS Code: exact-path 262144 > label-prefix
   131072 > label-anywhere 65536 > raw fuzzy) — a title hit ALWAYS beats an
   equally-fuzzy path/body hit.
4. **Learned usage layered on top of match score** — every shipping launcher:
   - PowerToys Run: final = base + weightBoost + SelectedCount×5 (persisted JSON)
   - Firefox: frecency w/ 30-day half-life decay + **adaptive input history**:
     the typed query string is associated with the result the user PICKED
     (use_count →10 asymptote via ×0.9+1, exact-query-match doubles score,
     ×0.975 daily decay, prune <0.1). Query→choice memory = the single
     highest-leverage mechanism.
5. **Typo tolerance (transpositions) as fallback**, but VS Code deliberately
   rejected typo-fuzzy to keep ranking stable/learnable — tier it, don't blend it.

### Latency
6. **100 ms keystroke→results is a CEILING, not a target** (Nielsen 0.1s
   instantaneous; sub-100ms deltas still perceivable per CHI 2015 JNDs 11-70ms).
   No spinner needed under 1s.
7. **Speed is won in architecture**: debounce expensive providers on a LONGER
   delay than cheap ones (PowerToys two-tier throttle), keep matching off the
   UI thread, index in background/out-of-process (Raycast: separate Rust indexer,
   NTFS MFT read). Python caveat: "matching is negligible" must be re-measured —
   reference impls are compiled.
8. **Pre-render before show** (Raycast keeps window alive at alpha-0, renders in
   rAF before reveal) — Qt analog: populate widgets before setVisible.

### Interaction & presentation
9. **Hotkey must toggle** (open→dismiss) ✅ Flint does this.
10. **Restore prior focus on close** — W3C ARIA modal requirement, not just lore.
11. **Every row gets an icon** ✅; **deliberately clip the last visible row** to
    signal scrollability (NN/g "illusion of completeness").
12. **Native-feel rules** (Raycast): no web affordances. BUT the no-hover rule is
    macOS-specific — Windows dark-app norms allow SUBTLE hover states.

### Evidence gaps (do not over-claim)
Zero-query design, prefix-modes vs unified search, section grouping, and concrete
dark-theme visual parameters produced NO surviving claims — decisions there are
judgment, not evidence.

## Flint gap analysis

| Practice | Flint today | Gap |
|---|---|---|
| Boundary-aware scoring | rapidfuzz WRatio (edit-distance) | ❌ no word-start/prefix bonuses in main pool (emoji provider already tiers prefix>substring>fuzzy) |
| Structural tiers | none in main pool | ❌ apps/notes/actions all compete on raw WRatio |
| Learned ranking | emoji recents only | ❌ no selection counts, no query→choice memory |
| Zero-query state | empty | ❌ nothing shown before typing |
| Two-tier debounce | ✅ sync pool instant, files 150ms | — |
| Toggle hotkey | ✅ | — |
| Focus restore | OS-default only | ⚠️ not explicit |
| Icons per row | ✅ | — |
| Clipped last row | ❌ hard 9-row cap, exact height | minor |
| Hover state | none | ⚠️ add subtle (Windows norm) |
| Latency budget | unmeasured | ⚠️ instrument keystroke→paint |

## Prioritized improvements

**P1 — ranking brain (the big one)**
1. `usage.json` learned layer: per-result selection count (PowerToys-style ×N boost,
   Firefox-style ×0.975/day decay) + **query→choice association** (what did I pick
   last time I typed "war"? Put it first).
2. Tiered scorer for the main pool: exact-prefix tier > word-boundary tier >
   substring tier > WRatio fuzzy tier (mirrors emoji provider; unjumpable tiers
   per VS Code).
3. Zero-query state: frecency-ranked recently-used apps/notes/actions (judgment
   call — evidence gap acknowledged).

**P2 — feel**
4. Explicit focus restore: capture foreground HWND at summon, restore on hide.
5. Subtle hover state (L4 @ ~40%) + latency instrumentation (log p95
   keystroke→render; assert <100ms).
6. Scroll affordance: raise visible cap, half-clip the last row.

**P3 — nice-to-have**
7. Aliases for apps ("browser"→Comet) via a user map in config.
8. Measure Python matching cost over the ~4.5k-entry pool; only optimize if >20ms.

---

## Addendum: rich-picker (dropdown) spec — researched + implemented 2026-07-13

Applied to the Claude Code session picker (SessionPopup). Key verified values:
two-line rows 52px (M3 menus + cmdk 40px one-line); metadata right-aligned;
max 6-8 visible rows then scroll (NN/g listboxes); type-to-filter mandatory
>15 items (NN/g); ARIA combobox keyboard contract (clamp Up/Down, Home/End,
Enter selects, Esc closes-without-select, first row pre-selected = Raycast
"sticky default"); skeleton rows not spinners (NN/g), show <300ms = nothing,
once shown hold >=400ms (Productboard); popup below-start-aligned +4px
(Radix), fade 150ms OutCubic in (shadcn/M3). Contextual ranking = BM25
k1=1.5 b=0.75 over title(x3)+content with recency boost, lifted from
an earlier session-search tool of mine.

Sources: W3C ARIA APG combobox, NN/g listbox-dropdown + skeleton-screens,
Raycast manual (action panel), cmdk raycast.scss, Radix/shadcn popover, M3
menus + motion tokens.
