<p align="center">
  <img src="docs/assets/hero-en.svg" alt="book-translate: source, living glossary and a translation with the same layout" width="100%">
</p>

<p align="center">
  <a href="https://github.com/Crabbb/book-translate/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Crabbb/book-translate/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-2a78d6">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-52514e"></a>
</p>

<p align="center">
  <a href="README.md">Русский</a> · <b>English</b>
</p>

Anyone can tell a model "translate this book". What comes back is smooth text where terms drift between chapters, the author's recommendations have become orders, and the layout, figures and index are gone.

**book-translate** works like an editorial office, not a prompt. It is a skill for LLM agents (Claude Code, Codex and others) plus a CLI, `bt`. Together they take a technical book or a work of fiction from English to Russian along the path a good publisher uses: preparation, a terminology glossary, translation, checking against the source, editing, independent review, typesetting.

The model is the translator here, not the whole process. Whatever code can check, code checks. Whatever needs understanding, the agent decides, and it records the reason.

## How it differs from "translate this book"

| | Prompt in a chat | Machine translation of a file | **book-translate** |
|---|---|---|---|
| One terminology across the book | no | manual glossary | **living glossary with reasons and case forms** |
| Context and the author's voice | within a window | within a phrase | **reads the whole book before translating** |
| Nothing lost, nothing invented | can't verify | can't verify | **accounts for every PDF glyph or translatable EPUB node** |
| Layout, tables, links, index | lost | partly | **rebuilt in the same design** |
| Text inside figures | stays English | stays English | **translated under the caption** |
| Prompts and code in books about LLMs | may run as instructions | break | **code untouched, prompt kept next to its translation** |
| Check against the source | just a claim | none | **for every paragraph, with evidence** |
| Independent review | none | none | **another model, with no access to the translator's reasoning** |
| Dropped session | start over | start over | **resumes from the same place** |

## The main danger of LLM translation: errors sound confident

Output from a large model reads smoothly. A meaning error looks as convincing as a correct sentence, and you cannot see it by reading only the Russian:

| Source | Smooth translation | What broke | Correct |
|---|---|---|---|
| `sensitive data` | важные данные ("important data") | The protected category changed | чувствительные данные |
| `should` | должен ("must") | A recommendation became an obligation | следует |
| `protecting against` X | защита X ("protecting X") | The direction of protection flipped | защита от X |
| `tamper-evident` | защищённый от подделки ("tamper-proof") | Detection became prevention | позволяющий обнаружить вмешательство |
| `intractable` | неразрешимый ("unsolvable") | A resource limit became an impossibility | практически не поддающийся решению |
| `performance` about speed | результативность ("effectiveness") | Promises quality instead of speed | производительность |

Asking a model to "translate more accurately" does not fix this. A process does, where each kind of error has a layer that catches it.

## Four layers of control

<p align="center"><img src="docs/assets/layers-en.svg" alt="Four layers: code checks facts, source check compares with the original, reading catches unnatural Russian, an independent judge finds what the translator cannot see in its own work" width="100%"></p>

A translator systematically misses its own errors, and that holds for a model as it does for a person. So the last layer is kept apart from the others: the judge sees only the source and the translation. It gives a diagnosis, and its findings become rules for the next chapter, so the process learns from the book itself.

## How a book goes through

The agent reads the entire book and prepares a glossary. For fiction it records chronology, characters, points of view, voices and forms of address. It consults sources when historical or cultural references affect meaning.

The agent then writes the book's plan and acceptance criteria, creates an active goal in the host and reads it back. The [managed workflow](skills/book-translate/references/workflow.md) opens translation only after these steps. Final acceptance for fiction EPUB requires [AC-01–AC-09](skills/book-translate/references/fiction-epub-acceptance.md). State is saved on disk.

## A living glossary, not a list at the front

<p align="center"><img src="docs/assets/bible-en.svg" alt="A glossary record and a chain: the decision changes, units become stale, evidence is voided, the agent fixes each one" width="100%"></p>

Only this book's glossary defines a term. If a decision changes on page 200, code finds every affected place. The translator does not have to remember what was decided in chapter one.

## Also built in

- **The author's voice.** A passport and voice profile fix how the reader is addressed, the authorial "we", and the strength of modal verbs.
- **No LLM stock phrases.** Rules catch the bureaucratese, clichés and calques that give machine text away.
- **Book text is always data.** Instructions inside a book about LLMs are never executed.
- **Nothing leaves on its own.** The CLI works locally: the book and translation stay in your working folder.
- **An honest summary.** The skill never reports "no errors": it states coverage and what was not checked.

## Quick start

Install [uv](https://docs.astral.sh/uv/) and use an agent whose host provides `create_goal` and `get_goal`.

```bash
git clone https://github.com/Crabbb/book-translate.git
cd book-translate
uv sync --locked
```

Open the repository in the agent and ask:

> Translate `book.epub` from English into Russian using `skills/book-translate/SKILL.md`. Use `work/mybook` as the working folder.

You can also start the CLI workflow and inspect its state:

```bash
uv run --locked bt workflow-start book.epub --work work/mybook
uv run --locked bt status --work work/mybook
```

The skill's procedures are written in Russian, its target language.

## What to know up front

- For reflowable EPUB 3, set `BT_EPUBCHECK` to a local EPUBCheck executable or JAR. Add `--pilot-chapter N --pilot-scene M` to `workflow-start` to try one scene. [EPUB details](skills/book-translate/references/epub.md)
- Tagged PDF requires a layout profile, Typst and fonts. Set the paths with `BT_TYPST` and `BT_FONTS`. [PDF build](skills/book-translate/references/build.md)
- If the host lacks goal tools, the workflow pauses at `goal` with preparation saved. Independent review requires a separate agent.

## What's inside

```text
skills/book-translate/
├── SKILL.md            route: 13 steps and when to stop
├── references/         the procedure for each step
├── rules/style.yaml    LLM stock phrases, bureaucratese, typography
├── profiles/           a publisher layout profile
├── templates/book.typ  the build template
├── bt.py               runs the CLI from any folder
└── tools/bt/           ingest, bible, qa, cite, gate, typeset, artifact
```

## Copyright

This repository contains only the tool, with no books or translations. You are responsible for your right to translate and distribute a given book.

## License

[MIT](LICENSE)
