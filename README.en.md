<p align="center">
  <img src="docs/assets/hero-en.svg" alt="book-translate: source, living glossary and a translation with the same layout" width="100%">
</p>

<p align="center">
  <a href="https://github.com/Crabbb/book-translate/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Crabbb/book-translate/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-2a78d6">
  <img alt="Tests: 172" src="https://img.shields.io/badge/tests-172-2a78d6">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-52514e"></a>
</p>

<p align="center">
  <a href="README.md">Русский</a> · <b>English</b>
</p>

**book-translate** is a skill for LLM agents (Claude Code, Codex and others) plus a CLI, `bt`. Together they translate a whole technical book from English to Russian, with consistent terminology, the layout preserved, and verifiable evidence for every paragraph.

The model is the translator here, not the whole process. Whatever code can check, code checks. Whatever needs understanding stays with the agent, and each of its decisions is recorded with a reason.

## Why not just ask a model to translate

Output from a large model reads smoothly, and that is the problem: a meaning error looks as confident as a correct sentence. Across a 300-page book this becomes systematic damage.

| What usually breaks | What book-translate does |
|---|---|
| One term gets three translations across chapters | A living glossary: one rendering per term, with a reason and case forms. Code checks the translation against it |
| The model softens the author: "must" for "should" | A book passport sets modality and voice rules, and a dedicated check catches drift |
| Text is lost or appears from nowhere | The book is split into units, accounting for every PDF glyph. A loss stops ingest |
| Layout, tables, links and the index disappear | Tagged PDF is parsed into structure and rebuilt: same styles, figures, code and links, with new pagination |
| Text inside raster figures stays in English | Every figure is viewed, and its transcription becomes a unit printed under the caption |
| Instructions inside a book about LLMs get executed | Book text is always data. Prompts in the book are kept next to their translation |
| "I checked everything" with no proof | A check is bound to a fingerprint of the text. When the translation or a term changes, the evidence is voided |

## How a book goes through

<p align="center"><img src="docs/assets/pipeline-en.svg" alt="Diagram: brief, PDF ingest, analysis, glossary; per chapter translate, check, edit, read, recheck, gate and judge; then cross-chapter pass, index and build" width="100%"></p>

State lives on disk, so work survives a dropped session: `bt status` recomputes it from files. Units arrive in packages, and before submission `bt cite` shows the package rules and links the translation to glossary terms.

## A living glossary, not a list at the front

<p align="center"><img src="docs/assets/bible-en.svg" alt="A glossary record and a chain: the decision changes, units become stale, evidence is voided, the agent fixes each one" width="100%"></p>

Only this book's glossary defines a term. If a decision changes on page 200, code finds every affected place. The translator does not have to remember what was decided in chapter one.

## What the first book showed

The skill was calibrated on a real IT book: 300 pages, 95 figures, around 86,000 words, a subject index. After all local checks, each part was read by an independent judge, a different model. It saw the text but not the translator's reasoning, and could only give a diagnosis.

<p align="center"><img src="docs/assets/judge-en.svg" alt="Chart: in round one the judge found 32 major and 48 minor defects across 11 parts" width="100%"></p>

<details>
<summary>The same data as a table</summary>

| Part | major | minor |
|---|---:|---:|
| Front matter | 3 | 7 |
| Chapter 1 | 2 | 5 |
| Chapter 2 | 1 | 5 |
| Chapter 3 | 3 | 3 |
| Chapter 4 | 4 | 5 |
| Chapter 5 | 4 | 3 |
| Chapter 6 | 3 | 3 |
| Chapter 7 | 1 | 4 |
| Chapter 8 | 3 | 4 |
| Chapter 9 | 2 | 5 |
| Chapter 10 | 6 | 4 |
| **Total** | **32** | **48** |

</details>

**The main finding is an uncomfortable one.** All 32 meaning errors passed the local checks and were found only by the judge. A translator checking itself systematically misses its own errors. So the judge is part of the process, and its findings become rules of the skill, so the next chapter starts with fewer of these errors. After fixes, round two gave 0 major in every part.

These are the kinds of errors involved. None of them stands out if you read only the Russian:

| Source | Smooth translation | What broke | Fixed |
|---|---|---|---|
| `sensitive data` | важные данные ("important data") | The protected category changes | чувствительные данные |
| `should` | должен ("must") | A recommendation became an obligation | следует |
| `protecting against` X | защита X ("protecting X") | The direction of protection flipped | защита от X |
| `tamper-evident` | защищённый от подделки ("tamper-proof") | Detection became prevention | позволяющий обнаружить вмешательство |
| `intractable` | неразрешимый ("unsolvable") | A resource limit became an impossibility | практически не поддающийся решению |
| `performance` about speed | результативность ("effectiveness") | Promises better quality, not speed | производительность |

The judge also found defects in the skill itself: a figure's caption split from its translation, contents placed before the cover, a figure lost after the index. All are fixed in code and covered by tests.

**Result:** 3341 units, 773 index references, 335 built pages, 0 findings from the automated PDF checks.

## Quick start

You need [uv](https://docs.astral.sh/uv/), [Typst 0.15.1](https://github.com/typst/typst/releases) and the fonts Libertinus Serif, Source Sans 3 and Liberation Mono. The source PDF must be tagged.

```bash
git clone https://github.com/Crabbb/book-translate.git
cd book-translate
uv sync
uv run pytest -q
```

Copy or link `skills/book-translate` into your agent's skills directory (for example, `~/.claude/skills/`). The CLI runs from any folder: `uv run <skill folder>/bt.py`, and uv installs its dependencies. Then ask:

> Translate `book.pdf` into Russian, working folder `work/mybook`.

The agent runs the process and asks you only when it cannot continue. To build, set the paths:

```bash
export BT_TYPST=/path/to/typst
export BT_FONTS=/path/to/fonts
uv run skills/book-translate/bt.py build book.pdf --work work/mybook
```

The skill's procedures are written in Russian, since its target language is Russian.

## What's inside

```text
skills/book-translate/
├── SKILL.md            route: 13 steps and when to stop
├── references/         the procedure for each step
├── rules/style.yaml    LLM stock phrases, bureaucratese, typography
├── profiles/           a publisher layout profile
├── templates/book.typ  the build template
└── tools/bt/           CLI: ingest, bible, qa, cite, gate, typeset, artifact
tests/                  172 tests
evals/                  scoring of judge verdicts
```

## Honest limits

- **Tested on one book.** A check on a second book, not used in calibration, is still ahead. Claiming it works on any book would be premature.
- **EN → RU only**, and Tagged PDF only. Other publishers need their own layout profile.
- **Quality depends on the judge.** Without it meaning errors remain: this was measured, not assumed.
- **It is neither free nor instant.** The book is read in full several times, which costs real tokens and time.
- **Scripts without fonts** (Arabic, Chinese, Devanagari) currently stay only in the figure rasters.
- The skill never reports "no errors": its summary states coverage and what was not checked.

## Copyright

This repository contains only the tool. No books, excerpts or translations are here, and they must not be added (`books/` and `work/` are in `.gitignore`). You are responsible for your right to translate and distribute a given book.

## License

[MIT](LICENSE)
