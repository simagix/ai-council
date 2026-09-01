# Use cases

Each file in this directory is one **question with its full context**.
This is how to ask the council something that needs more than a single
sentence — background, constraints, and what exactly you want decided.

## Format

Any text or Markdown file. The **entire file is sent verbatim** as the
question to every council member, so structure it however helps the
models: headings, bullet lists, tables — all fine.

A useful shape (see `mac-mini-storage.md`):

1. The question itself, stated plainly at the top
2. Context: background, how you work, what you already have
3. Constraints: budget, time, hard limits
4. The decision you want made, and what a good answer looks like

## Running one

```bash
ai-council --file use_cases/mac-mini-storage.md

# save the transcript too
ai-council --file use_cases/mac-mini-storage.md --save council.md

# pipe context in instead
cat my-question.md | ai-council --file -
```
