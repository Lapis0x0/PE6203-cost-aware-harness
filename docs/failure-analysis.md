# Failure analysis and component evidence

This appendix supports [the report](../report/REPORT.md). It answers three questions:

1. [Where the components came from](#1-baseline-failures-where-the-components-came-from): what failed under the default harness, and the benchmark leak we found on the way.
2. [What the trajectories show about each component](#2-component-evidence-in-the-ablation-trajectories), how large run-to-run noise is, and why costs moved.
3. [What still fails](#3-what-m3-still-fails-with-our-harness) with our full harness on MiniMax M3, classified into harness-level and model-level failures.

Every claim points to a task and a step. Trajectories are in the raw-log archives attached to the GitHub release: `logs-<config>.tar.gz` unpacks to `<config>/<task>/` with `result.json`, `trial.log`, `agent/trajectory.json` and `verifier/`. "Step N" is the `step_id` in `trajectory.json`; step 1 is the task prompt. For each task we use the last trial, the same rule as `scripts/export_results.py`. "Inferred" marks conclusions drawn from other runs of the same task rather than from verifier details; we did not download the benchmark's gold files.

## 1. Baseline failures: where the components came from

Before writing any component we read the trajectories of the 32 tasks that at least one default-harness baseline did not fully solve. The baselines at that point were DeepSeek V4.1 Flash, Claude Opus 5.5 and Qwen3.6-35B-A3B, our first weak model. MiniMax M3 replaced Qwen3.6 later (see the README), so **none of the components was designed from M3's failures**; the only M3-specific change came from the smoke test (section 2.6).

### 1.1 Findings

- Almost no run lost reward purely because of a harness defect. Flash and Opus failures were mostly domain mistakes (LibreOffice configuration keys, GIMP session files, background removal method, task interpretation) or likely grading ambiguity.
- Failures the harness could act on were concentrated in the weak model: submitting without checking, or with a check that did not test what the task asked; looping until timeout; sending keys to a dead terminal; hand-editing Office XML and breaking the file.
- Most failed runs did check their work, but checked the wrong thing: their own metric or their own state, not what the task and the checker cared about.
- Qwen3.6's long trajectories drove its cost: our provider gave it no cache discount, and every step re-sends the full history, so a single task could reach tens of millions of input tokens. This is provider pricing, not something a harness should fix, and it is why we replaced Qwen3.6 with M3.

### 1.2 Likely grading or task ambiguity (not harness failures)

All three models took nearly the same approach and scored 0, or the score did not track the approach.

| Task | Observation |
|---|---|
| 020 calculate-period-rate | All three divide the annual rate by periods per year and highlight the maximum; all score 0. |
| 022 monthly-sales-line-chart | All three write values plus an openpyxl line chart; the checker likely expects a formula or a specific chart structure (inferred). |
| 026 sales-cogs-column-chart | Flash and Opus build a correct-looking chart and both score 0. |
| 065, 067, 068 (slides) | All three satisfy the visible requirements. 068 asks about a "football club to-do list" that does not exist in the deck. |
| 027 add-dota-dlc-cart | Flash and Opus confirm the item is in the cart and still score 0. |
| 111, 112 (video) | Continuous scores; near-blank frames can score highest, so the grader separates poorly. |

### 1.3 Benchmark leak: `chrome_state.py solved`

**What.** Browser tasks ship `/usr/local/bin/chrome_state.py`, readable and executable by the agent. Its `task` mode opens the start page when the container starts; its `solved` mode navigates straight to a hard-coded answer URL and exists for the official oracle (`solution/solve.sh`). Nothing under `tests/` uses it. Twelve tasks in our subset ship it: 027, 028, 034, 035, 036, 037, 039, 041, 042, 045, 046, 048. A keyword scan of the other `environment/` folders found no similar leak.

**Who used it.** Flash and Opus read the script and ran `solved` in 027, 035, 041, 042 and 045; their successes on 041, 042 and 045 came mostly from it. Qwen3.6 never found it. Any "explore the environment first" component would have taught the weak model to find it too, turning a score gain into answer leakage.

**Fix.** `runner-patches/harbor-0.6.3-remove-chrome-state.patch` deletes the script as root after `task` mode has run. It applies to every configuration, changes no task or verifier, and is an evaluation fix, not part of our harness.

**Verification** with the official oracle on task 041:

| | Before fix | After fix |
|---|---|---|
| Oracle score | 1 | 0 |
| `solve.sh` output | jumps to the answer page with `solved` | `chrome_state.py: No such file or directory` |
| Tab the verifier sees | answer page | start page, so `task` mode still ran and Chrome is healthy |

We re-ran these 12 tasks for Flash and Opus and ran M3 only after the fix. Flash stayed at 70.8%; Opus fell from 67.0% to 62.0%. All reported numbers are after the fix.

### 1.4 Highlights by task family

**Office spreadsheets** (012 013 014 016 018 020 022 026; 17 failed runs).
- Tool misuse: 018 Qwen (openpyxl writes a formula but stores no value; the verifier reads empty), 014 Qwen (rewrote `workbook.xml` with ElementTree, changed the namespace prefix, corrupted the file), 016 Qwen (hand-written OOXML pivot, declared done after its own check failed).
- Misreading or shortcuts: 012 Qwen cleared cells instead of hiding them; on 013 all three built a static summary table instead of a real pivot.
- Every run answered the default harness's "are you sure?" by restating its previous conclusion without re-checking anything.

**Office documents and slides** (059 061 062 065 067 068 116 118 119; 15 failed runs).
- 062 Qwen: XML declaration on line 2, verifier `ParseError`, no well-formedness check before submitting; nested quotes in a heredoc froze the terminal for about 65 steps.
- 116 Qwen: a hand-written footer was not registered in `[Content_Types].xml`; it hit the same error at step 18 and treated it as noise.
- 059 Qwen: left the shell with Ctrl-D at step 99, then sent keys to a dead terminal for 65 steps until timeout (about 6.5M input tokens, $2.35; Flash solved the task in 17 steps for $0.02). 059 and 068 Qwen overwrote the original file without a backup and destroyed it.

**Web** (027 035 036 037 039 041 042 045; 11 failed runs).
- Qwen declared success with the evidence against it on screen: a page titled "Page not found" (045), "Current Weather" for a monthly forecast (039), a Cloudflare block (037).
- Observations were full of dbus and GCM noise and stale output; commands were sent before the previous one returned.

**System and media** (034 049 051 053 058 111 112; 13 failed runs).
- 049 Opus analysed for 38 minutes without writing any output and timed out ($3.70); Flash installed `rembg` and passed for $0.11.
- 111 Qwen repeated the same `curl` about 8 times, did not notice it had already produced the output at step 66, and kept working on Xvfb until timeout (183 steps).
- 111 Flash never read the `vlc` wrapper script the task named.

### 1.5 From failures to components

| Component | Failures it targets |
|---|---|
| C2 pre-submit check | Submitting without an independent check: 018, 014, 016, 062, 116, 045 (Qwen) most clearly. |
| C3 stall + budget | Loops and dead terminals: 059, 062, 111 (Qwen), 049 (Opus). Mainly saves cost. |
| C4 domain notes | Recurring tool mistakes: recalculate after openpyxl (018), back up before editing (059, 068), prefer high-level Office libraries (014, 016, 116), read a named tool's wrapper first (111). |
| C1 native tool calls + `write_file` | Heredoc quoting freezes (062) and unparsable JSON replies; plus reasoning continuity across turns, which the text protocol drops. |
| C5 output denoise | Browser, dbus and font noise in observations. Applied only to new observations, never to history, so prompt caching still hits. |

We did not build an "explore the environment" component because of the leak above, and did not rewrite history for context compression because it breaks cache hits for models that have caching.

## 2. Component evidence in the ablation trajectories

We read the trajectories of every flip between M3's default run (B0) and each single-component run (A1 to A5), and the cost differences.

### 2.1 Summary

- Trajectories show a component causing a flip in only a few places: **C2 on 018** (A2), **C3 on 048** (H-m3), and **C4 on 018** (A4, but A5 solved 018 without the note). For C1 and C5 we can confirm the behaviour changed, but found no flip we can attribute to them.
- Most single-component flips look like run-to-run noise or a different route through the task (section 2.4). Read the ablation table for direction, not as the net effect of each component.
- The full harness against B0 (13 gained, 2 lost, sign test p ≈ 0.007) is the strongest result, but one run per configuration cannot separate it from the chance that B0's single run was unlucky.

### 2.2 Per component

**C2 pre-submit check (A2, 59.7%).** Strong evidence on 018. B0 writes `=D3*F3*24`, recalculates a copy in `/tmp`, reads the right value, and submits the original file, which has no stored value. A2 does the same, and when it first declares completion the check reports:

> step 6: `PROBLEM /app/Multiply_Time_Number.xlsx: 2 formula cells have no cached value ... recalculate and save with LibreOffice`

At step 10 the model copies the recalculated file back to `/app`, the check turns OK, and the task passes. Across all M3 runs of 018, every run that copied the recalculated file back passed and every run that did not failed. The checklist text helped separately on 118: A2 wrote an independent XML check and found leftover Georgia and Arial fonts it had missed. Counter-example: on 012 the check also found missing values and the model fixed them, but the task still scored 0.

**C3 stall + budget (A3, 49.7%).** In A3, C3 fired only 10 times across 40 tasks, and 5 of A3's 8 flips had no C3 event at all. The clearest case is H-m3 on 048: blocked by the site, the model was about to submit an archived copy (step 181: "I think the archive.org approach is the best we can do"), then cited the step-back hint (steps 170 and 172), tried a different user agent, reached the real page at step 184 and passed. A3's lower cost ($3.32 → $2.30) came mostly from two tasks where B0 happened to run long and C3 never fired.

**C4 domain notes (A4, 49.4%).** On 018 the model echoes the note almost word for word ("openpyxl does not compute formulas, so the cached value won't be available without LibreOffice recalculating") and passes. A5 did the same without the note, so this is not unique to C4. A4 also cost more: 014 went from 22 to 210 steps.

**C1 native tool calls (A1, 53.8%).** Behaviour changed clearly: about one command per step instead of 1.38, median 56 steps instead of 23, reasoning kept on 62% of steps instead of 7%, and `write_file` used on 33 tasks. On 051, B0 decided at step 27 that the task was impossible without a display; A1 checked for Xvfb, started it and passed. That fits C1's intent, but other configurations also found Xvfb, so we do not claim C1 caused it. The text-protocol failures C1 was meant to fix were rare for M3 (section 2.3), so A1's gain is not "fixed formatting errors".

**C5 output denoise (A5, 56.1%).** No evidence of effect. Noise lines made up only 1.2% of B0's observation characters and appeared in 6 tasks. A5's flips occurred on tasks that configurations without C5 also solved.

### 2.3 The text protocol was not M3's problem

Of B0's 1,321 steps, 6 (0.45%) were lost to parse errors and 18 (1.36%) carried format warnings, all 18 false positives (a first command of `C-c` or an empty wait). A2 to A5 use the same text protocol and show the same rates. Under native tool calls, A1 and H-m3 lose 0.19% and 0.53% of steps to "No tool call found" or over-long output.

### 2.4 How large is the noise?

- On the 34 tasks where C3 never fired, A3 should behave like B0, yet 7 tasks flipped (5 up, 2 down). Roughly one task in five flips between two runs that are effectively identical.
- Twelve tasks failed in B0 but passed in at least four of the other six M3 configurations. Where the trajectory shows why, B0 took a route the verifier does not accept: a different but equivalent URL (028, 048), not creating a virtualenv to install a tool (049), giving up because no display was running instead of starting Xvfb (051), or a different configuration path (058).
- A systematic pattern: under the default harness M3 stops early. 17 of B0's 27 failures end within 30 steps, and none times out.
- Eleven tasks fail in all seven M3 configurations, independent of components.

### 2.5 Why cost moved

**M3 with our harness costs twice as much as B0** because it takes twice as many steps; the cost per step does not change.

| | B0-m3 | H-m3 | Ratio |
|---|---|---|---|
| Steps | 1,321 | 2,838 | 2.15 |
| Commands (excluding submit) | 1,820 | 2,688 | 1.48 |
| Input tokens | 40.2M | 95.1M | 2.37 |
| Cost | $3.32 | $7.00 | 2.11 |
| Cost per step | $0.00251 | $0.00247 | 0.98 |

The extra steps come from one command per turn under C1, fewer early stops (tasks ending within 12 steps fell from 14 to 2), 217 steps after the pre-submit check (7.6%), and a long tail: eight tasks account for 60% of the extra steps and 85% of the extra cost. More than half the extra money went to tasks that still failed.

**Opus became six times cheaper** not because of C3, which never fired on its expensive tasks, but because each step got far shorter: average output fell from 1,761 to 259 tokens per step.

| Task | Default harness (steps / time / output tokens / cost) | Our harness |
|---|---|---|
| 042 | 135 / 2400 s timeout / 50k / $2.19, failed | 24 / 309 s / 6.9k / $0.25, passed |
| 016 | 44 / 2400 s timeout / 178k / $4.31, passed | 9 / 246 s / 4.3k / $0.15, passed |
| 049 | 32 / 2400 s timeout / 155k / $3.70, failed | 9 / 136 s / 2.6k / $0.09, passed |
| 059 | 37 / 1162 s / 69k / $1.89, passed | 7 / 121 s / 2.2k / $0.10, passed |
| 062 | 25 / 1021 s / 74k / $1.84, passed | 9 / 120 s / 2.8k / $0.14, passed |

On 016 and 049 the default run spent 4,000 to 4,600 output tokens per step and ran out of time after 32 to 44 steps. On 042 the default run hit a CAPTCHA page at step 11 and then spent 99 of its 135 steps without a tool call, waiting for input that never came; our run did not meet the CAPTCHA, so we cannot credit the harness for that one.

### 2.6 The smoke test

Before the full runs we ran four tasks on M3 with the full harness and read every step. M3 left 40 to 60% of its `bash` calls without a trailing newline, so the commands were typed and never executed. We changed `bash` to press Enter unless told not to, added an offline test, and re-ran the smoke test before starting the 11 configurations. This is the only change we made for M3 specifically.

### 2.7 What the pre-submit check caught

| Config | Tasks gated / times | Tasks with a file report / reports | Reports with PROBLEM | Fixed after | Passed after fix |
|---|---|---|---|---|---|
| A2-m3 | 39 / 78 | 16 / 32 | 2 (012, 018) | 2 | 1 (018) |
| H-m3 | 40 / 80 | 17 / 34 | 0 | none | none |
| H-opus55 | 40 / 75 | 17 / 31 | 0 | none | none |

Both problems were formula cells without stored values. In H-m3 the same problem was already handled before the gate, most likely because of C4's openpyxl note, so C2 and C4 overlap. Steps after the gate: 14.7% of A2's steps and 7.6% of H-m3's.

## 3. What M3 still fails with our harness

H-m3 scores below 1 on 15 tasks. **None of them fails mainly because of the harness**: 9 are model-level failures and 6 are tasks where grading is in doubt.

| Type | Subtype | Count | Tasks |
|---|---|---|---|
| Harness-level | premature termination, lost context, unparsable tool calls, no retry after a recoverable error, problems introduced by a component | 0 as main cause | none |
| Model-level | misreading the task or taking a shortcut | 3 | 012, 042, 067 |
| Model-level | misusing a domain tool, including wrong configuration knowledge | 4 | 013, 016, 058, 061 |
| Model-level | wrong plan | 2 | 037, 111 |
| Grading in doubt | the checker requires something the task does not state, or environment and verifier disagree | 6 | 020, 026, 027, 065, 068, 112 |

### 3.1 Per task

| Task | Why it failed | Type | Key evidence |
|---|---|---|---|
| 012 | "Hide N/A" done with a white conditional font; the checker requires hidden rows. H-flash passes. | Misreading | step 6: "I'll use conditional formatting with a white font color" |
| 013 | Built a real pivot with UNO, could not format it, fell back to a static table; the checker compares pivot fields. | Tool misuse | step 127: "Let me just go with the clean data tables approach" |
| 016 | Macro hung; wrote the pivot by hand without `<items>` in `pivotField`, unlike H-flash's passing file (inferred). | Tool misuse | steps 48–53, 131–134 |
| 020 | All 11 configurations score 0 with the same approach. | Grading in doubt (inferred) | step 4: "Period Rate = Annual Rate / Period per year" |
| 026 | All 11 configurations score 0; H-m3 also referenced a copied sheet. | Grading in doubt (inferred) | step 12 |
| 027 | The verifier's cart page lacks the item in all five configurations checked, including two that saw it in their own cart view. | Environment vs verifier | `page_info.json` |
| 037 | Cloudflare never let M3 through; it guessed URL parameter names from memory and guessed wrong. | Wrong plan | step 225: "Let me just stick with what I have" |
| 042 | Put "black" and "sale" into the search query; the filter chips were never selected. A printed "All filters applied: True" was hard-coded. | Shortcut | steps 112–113 |
| 058 | Wrote the autosave interval as 180 seconds; the key is in minutes and must be 3. | Configuration knowledge | step 16: "(in seconds)" |
| 061 | Added a floating table with placeholder text instead of filling the content placeholder. H-opus55 passes. | Tool misuse | step 8 |
| 065 | Same geometry as H-flash, both 0; the gold file likely keeps the aspect ratio (inferred). | Grading in doubt | step 26 |
| 067 | Took "the first text box" in XML order rather than visual order. | Misreading (inferred) | step 33 |
| 068 | The task's to-do list does not exist in the deck; models picked different slides. | Task ambiguity | step 6 |
| 111 | Never read the `vlc` wrapper the task named, though step 3 listed it; built a GNOME background by hand instead. B0 read it and scored 0.44. | Wrong plan | steps 3, 138–141 |
| 112 | The task says "a scene"; the checker compares against one specific frame. All 11 configurations get partial scores of 0.34 to 0.75. | Grading in doubt | step 14 |

On 037 and 111, where B0 did better than H-m3, we found no component behind the difference. On 037 every M3 run guessed URL parameters independently and only B0 guessed right. On 111, runs that did and did not read the wrapper are spread across configurations with and without each component.

### 3.2 Example of a harness-level weakness: 016, steps 75–83 and 155–158

Even here the harness is a secondary factor, not the cause. A LibreOffice macro hung in the foreground at step 75. M3 sent seven more commands from step 76 to 82. Our freeze detection compares a hash of the whole tmux pane, and the echoed input changed the pane every step, so the automatic Ctrl-C never fired; the model sent `C-c` itself at step 83. At step 155 the pre-submit check reported "no problems found", which only means the file opens, and at step 158 the model cited it as proof that the pivot was correct. Two lessons: freeze detection must ignore echoed input, and the check's report must say plainly that it does not verify content. The task was already lost to the hand-written pivot before either point mattered.

### 3.3 Example of a model-level failure: 058, steps 16–18

The task asks for a LibreOffice autosave interval of 3 minutes. At step 16 M3 reasons that `AutoSaveTimeIntervall` is "(in seconds)", writes 180, and at step 18 prints "interval 180s (3 minutes)". The key is in minutes, so the checker reads 180 and fails the task. The plan was reasonable, the execution was clean and the model verified its work, but it verified its own wrong assumption; no generic harness check can know the unit. A2 to A5 solved the same task, so this is chance rather than a hard limit of the model.
