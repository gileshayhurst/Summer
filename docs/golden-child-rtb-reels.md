# Golden Child — RTB sizzle reels

Six reels, one per positioning angle, cut from the 8 interviews in
`C:\Users\giles\Downloads\Forven Videos`. Generated 2026-08-26.

## What these reels are (and are not)

Participants were **never shown the RTB claims verbatim** — no "40% less fat",
no "leaner dogs live two years longer". They were probed on drizzles,
human-grade wording and bowl presentation, but not on the master RTB library.

So these are not proof that the claims land. They are proof that **the need each
RTB answers is real**, in customers' own words — including where customers push
back. Each prompt deliberately collects both the demand signal and the friction
on its theme, so the reels read as research rather than a validation montage.

## Prep

Transcripts were encoded to rich tier before analysis:

```
python -m encoder "C:\Users\giles\Downloads\Forven Videos" --in-place --model tiny
```

Match rates: 87–96% on seven interviews. `4e7ccf39` scored 52.0% — it is the
known-truncated 8:21 recording of a 14:15 conversation (see CLAUDE.md); the
missing text simply is not in the video, so it contributes fewer clips.
Originals preserved as `<stem>.forven.txt`.

## Selection rule

Every scored segment across all 8 interviews is ranked by score, then:

- score floor 7 (relaxed to 5 only if a theme returns under 5 clips or under 85s)
- max 3 clips per respondent, so no single voice owns a reel
- stop at 16 clips or ~110s

## The reels

| # | Reel | Clips | Length | Voices | Scores |
|---|------|------:|-------:|-------:|--------|
| 01 | See the Difference | 12 | 114s | 5 | 9×1, 8×7, 7×4 |
| 02 | Elevated Dining Experience | 11 | 111s | 5 | 9×2, 8×4, 7×5 |
| 03 | Variety and Customization | 9 | 111s | 5 | 9×1, 8×8 |
| 04 | Convenience Without Compromise | 11 | 126s | 5 | 9×2, 8×5, 7×4 |
| 05 | Functional Results | 12 | 120s | 5 | 9×2, 8×6, 7×4 |
| 06 | Longevity | 10 | 98s | 4 | 9×2, 8×4, 7×4 |

Each has a WebVTT sidecar beside it for soft captions.

## Prompts

Reusable as-is in the app. House style from `prompt_history.json`. None uses the
phrase "positive opinions" — that wording triggers the analyzer's positive-only
filter and would drop the pushback.

**01 See the Difference**
> Find moments where the participant reacts to how dog food looks and what is visibly in it - being able to see real, recognisable pieces of meat and vegetables in the bowl, judging a food by its ingredient list and whether the ingredients sound like real food, how the food smells compared to human food or typical dog food, comparing kibble's appearance to fresh or homemade food, and their gut reaction to words like "human-grade" and "fresh-frozen". Include both enthusiasm about visible real ingredients and scepticism that a beautiful bowl or human-grade wording matters for a dog.

**02 Elevated Dining Experience**
> Find moments where the participant describes mealtime as a shared moment with their dog - how the dog reacts and gets excited at feeding time, what the participant feels watching their dog eat, feeding as an expression of love or pampering, sharing food from the kitchen or the table, and whether making a meal feel special or restaurant-like appeals to them. Include both genuine emotion or delight and views that mealtime is purely functional and does not need elevating.

**03 Variety and Customization**
> Find moments where the participant talks about variety in their dog's food - the dog getting bored, picky, or losing interest in the same meal, mixing in toppers, sauces, drizzles, wet food or leftovers to dress food up, rotating flavours or proteins, and their reaction to choosing or combining different mains and drizzles. Include both interest in variety and customisation and the view that dogs do not need variety or that mixing things in is unnecessary.

**04 Convenience Without Compromise**
> Find moments where the participant talks about the practical effort of feeding - how quickly meals come together on a busy day, scooping or pre-portioned servings, subscription or auto-ship delivery, fridge and freezer space, thawing, storage, smell in the house, bowls, mess and cleanup, and how much ease influences what food they buy. Include both relief at whatever makes feeding easier and objections that frozen or fresh food is impractical, takes up space, or needs planning ahead.

**05 Functional Results**
> Find moments where the participant links their dog's food to visible health results - digestion, stools, gas, upset stomach, itching, allergies, skin and coat condition, shedding, joints and mobility, energy levels and weight, and food changes made on a vet's advice or to fix a problem. Include both stories of a food visibly improving or harming their dog's health and doubt that any food really delivers those results.

**06 Longevity**
> Find moments where the participant talks about their dog's lifespan and future - wanting as many years together as possible, worries about their dog ageing, slowing down or gaining weight, seeing food as an investment in a longer and healthier life, and what they would change to buy more good years. Include both emotional statements about time together and scepticism that food meaningfully changes how long a dog lives.

## Known issue: mixed clip orientation

`1a376d5e` is the only portrait interview (720×1280); the other seven are
landscape (1280×720). Reels 01, 03 and 04 therefore start on a portrait clip and
concat landscape clips after it, and `stitch_clips` uses `-c copy`.

Measured: the files decode end to end with no errors and each clip renders at its
own correct aspect. But the container reports the *first* clip's dimensions, so
a player that locks to those may letterbox or stretch the rest. If that shows up
in review, the fix is to scale and pad every clip to one size in `extract_clip`.
Not done here — it changes output for every user of the tool.

## Repo change made for this run

`video_editor.extract_clip` gained a `width` parameter and now shrinks any
identification-overlay line that would overrun the frame. Forven export stems are
~53 characters; at the base font size they rendered wider than a 720px frame and
were cropped at both edges to an unreadable middle slice. `generator_app` already
computed `width` at the call site and discarded it. Full suite passes (491).
