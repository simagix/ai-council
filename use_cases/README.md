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
python ai_council.py --file use_cases/mac-mini-storage.md

# the HTML transcript is written by default (out/council-<timestamp>.html);
# --md/--html pick Markdown and/or an exact filename — combinable
python ai_council.py --file use_cases/mac-mini-storage.md
python ai_council.py --file use_cases/mac-mini-storage.md --md council.md --html council.html

# pipe context in instead
cat my-question.md | python ai_council.py --file -
```
