# n8n Script node reference

This repo only sources and dedups raw experiments (`data/experiments.json`).
The actual Gemini scripting call happens in your existing n8n Script node,
not here. Pull unused records from `data/experiments.json` (filter on
`used: false`) and feed each one through the prompt below.

## Gemini system prompt

Paste this as the system instruction in the Script node's Gemini call:

---

You write short-form video scripts for an educational psychology channel.

HARD RULES, no exceptions:
1. No contractions anywhere. Write "are not" never "aren't", "does not" never "doesn't", etc.
2. No generic filler or punch-tag lines (e.g. do not end with a vague dramatic one-liner tacked on for effect).
3. Simple English a 5th grader can understand. Short sentences. No jargon.
4. The hook (first line) must use a proven viral hook template: it should contradict something the viewer
   assumes is true, and create a clear, specific curiosity gap. It must NOT be a generic phrase like
   "here's proof" or "you won't believe this." It should sound like a real, specific claim.
5. The lesson (final line) must flow as a natural continuation of the story using a connector like
   "This shows that..." — never labeled "Lesson:" and never a short punchy tag added just for effect.
6. Total script length: 20-40 seconds spoken aloud, meaning roughly 55-100 words total.
7. Base the story on the real, documented experiment given below. Do not invent details not implied by the summary.

Return ONLY valid JSON, no markdown fences, no preamble, matching exactly this shape:
{"hook": "...", "body": "...", "lesson": "..."}

---

## n8n Code node — validation (drop right after the Gemini call)

```javascript
const CONTRACTIONS = /\b(don't|doesn't|didn't|can't|won't|wouldn't|shouldn't|couldn't|isn't|aren't|wasn't|weren't|it's|that's|there's|he's|she's|they're|you're|we're|i'm|i've|you've|they've|we've)\b/i;

const items = [];

for (const item of $input.all()) {
  const script = item.json; // expects { hook, body, lesson }
  const fullText = `${script.hook} ${script.body} ${script.lesson}`;
  const violations = [];

  if (CONTRACTIONS.test(fullText)) {
    violations.push(`contains contraction: ${fullText.match(CONTRACTIONS)[0]}`);
  }

  const wordCount = fullText.trim().split(/\s+/).length;
  if (wordCount < 55 || wordCount > 100) {
    violations.push(`word count ${wordCount} outside 55-100 range`);
  }

  const lessonLower = script.lesson.trim().toLowerCase();
  if (lessonLower.startsWith('lesson:') || lessonLower.startsWith('the lesson is') || lessonLower.startsWith('moral:')) {
    violations.push('lesson uses a labeled tag instead of a natural connector');
  }

  if (violations.length > 0) {
    // Hard fail, matching studio convention: do not pass broken
    // scripts downstream to Builder/TTS.
    throw new Error(`Script failed validation: ${violations.join('; ')}\n${JSON.stringify(script)}`);
  }

  items.push({
    json: {
      ...script,
      full_text: `${script.hook}\n\n${script.body}\n\n${script.lesson}`,
      word_count: wordCount,
    },
  });
}

return items;
```

## Marking an experiment as used

After a script clears validation, flip `used: true` on that experiment's
record in `data/experiments.json` and commit it back via the GitHub API
(same pattern as your R2+Dispatch node writing state back). This keeps
the guard/dedup logic consistent with the rest of the studio instead of
tracking used-state separately in n8n.
