<p align="center">
  <img src="docs/assets/editorial-hero.webp" alt="book-translate. Two open books, a technical manual and a work of fiction" width="100%">
</p>

[![CI](https://github.com/Crabbb/book-translate/actions/workflows/ci.yml/badge.svg)](https://github.com/Crabbb/book-translate/actions/workflows/ci.yml) · [MIT license](LICENSE) · [Русский](README.md)

# Translate books from English into Russian

**book-translate** is an LLM agent skill and a CLI, `bt`, for translating technical books and fiction. The agent reads the book, records its editorial decisions, then takes each chapter through source comparison, editing and acceptance. Code enforces the sequence and saves progress so work can resume at the expected step.

## One workflow, two editorial approaches

| | Technical books | Fiction |
|---|---|---|
| What the agent studies | Terminology, domain relationships, authorial voice, examples and book structure | Characters, chronology, point of view, voices, forms of address and recurring motifs |
| What the translation preserves | Precise instructions, the distinction between advice and requirements, consistent terms and the meaning of code examples | Distinct dialogue, subtext, rhythm, wordplay and the order in which the story reveals information |
| How the result is checked | Review terms, numbers, links and examples. For tagged PDFs, check the rebuilt layout | Compare scenes and dialogue to the source. Fiction EPUB uses [nine mandatory acceptance criteria](skills/book-translate/references/fiction-epub-acceptance.md), covering both editorial quality and file structure |
| Example decision | **Should** in a manual stays a recommendation when the author sets no requirement | A shift between Russian **ты** and **вы** is checked against the characters’ relationship and the whole scene |

When a historical detail, form of address or cultural reference affects meaning, the agent consults sources and records its decision. In a technical book, changing a term flags the affected passages for review. [Fiction guidance](skills/book-translate/references/fiction.md) · [Glossary and decision rules](skills/book-translate/references/bible.md)

## Code enforces the required stages

![Four stages with checkpoints between them. Read the book, set the plan and goal, translate and edit, then deliver the finished book](docs/assets/editorial-workflow.webp)

1. **Study the book.** The agent records the brief, ingests the source, checks a draft English build, reads the full translation scope and prepares the glossary.
2. **Set criteria and a goal.** The agent ties the book's acceptance criteria to specific passages and risks, then creates and confirms an active goal in the host.
3. **Translate and edit.** The CLI opens translation batches after preparation. Each chapter goes through translation, source comparison, editing, reading in Russian and a recheck.
4. **Accept the result.** The CLI checks that the review covers the plan, that evidence is current and that the required reports are present. The finished book is checked after it is built.

When evidence for a required step is missing or stale, `bt` blocks the next one. `bt status` shows where to resume, even in a new session. Code checks order, coverage and hashes. Editorial review evaluates meaning, voice and the quality of the Russian prose. [Workflow and transition rules](skills/book-translate/references/workflow.md)

## Quick start

Install [uv](https://docs.astral.sh/uv/) and open the repository in an agent whose host provides `create_goal` and `get_goal`.

```bash
git clone https://github.com/Crabbb/book-translate.git
cd book-translate
uv sync --locked
```

> Translate `book.epub` from English into Russian using `skills/book-translate/SKILL.md`. Use `work/mybook` as the working folder.

To inspect an existing book workflow:

```bash
uv run --locked bt status --work work/mybook
```

- **Reflowable EPUB 3.** Point `BT_EPUBCHECK` at a local EPUBCheck installation to validate the build. To try one scene, use `workflow-start` with `--pilot-chapter N --pilot-scene M`. [EPUB guide](skills/book-translate/references/epub.md)
- **Tagged PDF.** Building a PDF requires a layout profile, Typst and fonts. Use `BT_TYPST` and `BT_FONTS` to set the paths. [PDF build guide](skills/book-translate/references/build.md)
- **Host tools and review.** If the host lacks goal tools, the workflow pauses at `goal` and saves the preparation. An additional agent is needed for independent review. [Independent review](skills/book-translate/references/judge-optional.md)

[Open the skill instructions in Russian](skills/book-translate/SKILL.md)

## License

The code is distributed under the [MIT License](LICENSE). Book sources and translations stay in your working directory. You are responsible for the rights to translate or distribute a specific edition.
