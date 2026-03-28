---
name: pptx-agents
description: >
  Agent role definitions for generating PPTX slides. Each .md file defines
  a specialized sub-agent persona (cover, TOC, content, section-divider,
  summary) that follows ppt-orchestra-skill to produce individual slide JS
  modules compiled into a final presentation.
category: document-generation
triggers: [pptx, ppt, powerpoint, slides, presentation, deck, report]
---

# PPTX Slide Agent Roles

This directory contains role definitions for slide generators.
Each file describes a sub-agent persona responsible for generating one type of slide.

| Agent | File | Purpose |
|-------|------|---------|
| Cover Page | `cover-page-generator.md` | Opening slide with title, subtitle, visual |
| Table of Contents | `table-of-contents-generator.md` | Navigation/overview slide |
| Section Divider | `section-divider-generator.md` | Transition between major sections |
| Content Page | `content-page-generator.md` | Main content slides (text/data/comparison/timeline) |
| Summary Page | `summary-page-generator.md` | Closing slide with takeaways and CTA |

## Usage

1. Read `ppt-orchestra-skill/SKILL.md` for overall planning
2. Read the relevant agent file to understand layout rules and design principles
3. Use `slide-making-skill/SKILL.md` to generate the actual PptxGenJS code
4. Use `color-font-skill/SKILL.md` to pick palette and fonts
