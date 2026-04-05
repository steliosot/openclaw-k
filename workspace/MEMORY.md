# MEMORY

## User preferences

- For ChatGPT-generated images: save the original image to the workspace, then send that original file. Do not send UI screenshots.

- Persistent rule from user: Do not generate with Nano Banana or Veo unless the user gives explicit final approval right then (clear "üret/yes"). Always ask first and wait for confirmation.
- Persistent budget rule from user: Do not exceed Google Search API free daily quota (100 searches/day) without asking user first and getting explicit approval.
- Persistent workflow rule: After drafting/sending a ChatGPT message via browser automation, always perform a post-send verification and confirm it was sent before continuing.
- Persistent workflow preference: Maintain continuous momentum during iterative production ("durmuyorum"); avoid stalling and keep progressing scene-by-scene.
- Persistent rule from user: Do not generate any image without asking first and receiving explicit approval.
- Persistent troubleshooting rule: If Gemini/Google API auth appears missing in terminal runtime, first run `source ~/.zshrc` and then re-check auth before deeper debugging.
- Contact/context note: Ilker provided technical support in this session and handed over back to Zehra.
- New production rule from user: For video prompts, always include strict anti-spawn and continuity constraints (explicitly include "no extra person/people") and reuse the locked project prompt structure/constraints consistently across iterations.
- Persistent workflow requirement: Do not forget previously agreed project briefs/constraints across turns or scenes; carry forward locked brief details unless user explicitly changes them.
- New hard video workflow rule from user: Before every video generation, first create/prepare the scene-specific first frame and get explicit user confirmation; do not generate video before first-frame confirmation.
- Communication rule from user: If uncertain, do not invent or guess; explicitly say you are not sure.
- Persistent accuracy rule (added after Ilker support): Do not claim feature impossibility without verification. For Veo/4K and other capability questions, state route-specific limits vs model capabilities explicitly; if uncertain, say uncertain and suggest/perform verification (and escalate with Ilker when needed).
- Hard rule from user: Never use a derived first frame for video generation; only use the exact user-approved first frame.
- Prompt workflow rule from user: Do not write prompts by rote; verify scene facts first, think/check for contradictions, and always explicitly reconfirm which exact first frame will be used before every generation.
- Hard execution rule from user: Before EVERY video run, do a preflight checklist in-chat: (1) exact first-frame filename/path, (2) duration/resolution, (3) audio on/off expectation. Do not run until user confirms this checklist.
- Absolute first-frame rule from user: Never generate video with any frame other than the exact user-approved first frame. Never derive/extract/insert alternate first frames.
- Absolute audio rule from user: Unless user explicitly says "sesli üret", do not deliver audio. Default to silent output (strip audio if route cannot generate silent natively).
