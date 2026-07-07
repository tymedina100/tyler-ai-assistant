# VAN-24 README Review Artifact

PR: https://github.com/tymedina100/tyler-ai-assistant/pull/32  
Branch: `ai/assistant/van-24-core-workflows`

This file is the review fallback Vera requested: the exact README section/diff for the VAN-24 scope decision is reproduced inline so the issue can be reviewed without relying on PR summary text alone.

## README.md changed section

```md
# Tyler AI Assistant

A focused AI operating assistant for Tyler: it turns Linear priorities, Career Ops workflows, and action-oriented calendar/task support into concrete next steps without trying to become a general-purpose everything app.

## MVP core workflows

The MVP is intentionally limited to three weekly-use workflows:

1. **Project planning and daily priorities from Linear.** The assistant helps Tyler understand active Linear work, identify the next useful action, and keep project execution moving.
2. **Job search support through Career Ops / approval-based workflows.** The assistant supports resume, application, networking, and outreach work, but keeps external-facing actions approval-based.
3. **Calendar, tasks, and routine support only where it helps Tyler act.** Calendar, Todoist, reminders, and routines are in scope when they reduce friction on a concrete priority.

Everything else is later unless Tyler uses it weekly. Extra agents, Telegram modes, Company Mode, Google/Todoist/GitHub/Linear helpers, product workflows, publishing, finance, marketing, and other experiments should stay treated as supporting infrastructure or later V1+ scope unless they directly serve one of the three workflows above.

The assistant is not trying to be a do-everything autonomous company, full productivity suite, or new-agent factory right now.
```

## PR diff excerpt

```diff
-# Tyler AI Assistant
-
-A beginner-friendly command-line AI assistant built with Python and the OpenAI API.
+# Tyler AI Assistant
+
+A focused AI operating assistant for Tyler: it turns Linear priorities, Career Ops workflows, and action-oriented calendar/task support into concrete next steps without trying to become a general-purpose everything app.
+
+## MVP core workflows
+
+The MVP is intentionally limited to three weekly-use workflows:
+
+1. **Project planning and daily priorities from Linear.** The assistant helps Tyler understand active Linear work, identify the next useful action, and keep project execution moving.
+2. **Job search support through Career Ops / approval-based workflows.** The assistant supports resume, application, networking, and outreach work, but keeps external-facing actions approval-based.
+3. **Calendar, tasks, and routine support only where it helps Tyler act.** Calendar, Todoist, reminders, and routines are in scope when they reduce friction on a concrete priority.
+
+Everything else is later unless Tyler uses it weekly. Extra agents, Telegram modes, Company Mode, Google/Todoist/GitHub/Linear helpers, product workflows, publishing, finance, marketing, and other experiments should stay treated as supporting infrastructure or later V1+ scope unless they directly serve one of the three workflows above.
+
+The assistant is not trying to be a do-everything autonomous company, full productivity suite, or new-agent factory right now.
```

## Acceptance criteria mapping

- Names the 3 core workflows: covered in `## MVP core workflows`.
- Marks all other agents/features as later unless used weekly: covered by the "Everything else is later" paragraph.
- Short sentence explaining what the assistant is not trying to do right now: covered by the final sentence.
- Reflected in project description: covered by the revised top README description.
