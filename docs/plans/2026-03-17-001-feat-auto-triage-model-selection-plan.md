---
title: "feat: Auto-triage issues to Sonnet or Opus based on complexity analysis"
type: feat
status: active
date: 2026-03-17
---

# Auto-Triage Issues to Sonnet or Opus Based on Complexity Analysis

## Overview

Replace the hardcoded `claude-sonnet-4-6` model selection with a lightweight Sonnet triage call that analyzes the issue content and routes to the appropriate model. Single `agent` label for users — the system decides the model.

## Problem Statement

`_select_model()` always returns `claude-sonnet-4-6`. Complex issues that need Opus get processed by Sonnet, which either fails or produces shallow results. The agent then self-triages mid-execution (adding `claude-opus` labels), which is wasted work and creates re-trigger loops.

## Proposed Solution

Before dispatching the real worker, spawn a fast Sonnet triage call (~5s, ~$0.005) that reads the issue title + body and returns SIMPLE or COMPLEX. Route accordingly.

## Implementation

### 1. Add `title` and `body` to QueueItem (`queue.py`)

The webhook payload already sends these fields. Store them for triage.

### 2. Pass title/body from webhook handler (`server.py`)

The issue handler at line 282 creates a QueueItem without title/body despite the payload having them.

### 3. Implement LLM triage in `_select_model()` (`dispatcher.py`)

Replace the hardcoded return with a subprocess call to `claude --print --model claude-sonnet-4-6` with a triage prompt. Parse the one-word response.

Triage prompt:
```
You are a complexity classifier for GitHub issues. Given the issue title and body,
classify as SIMPLE or COMPLEX.

SIMPLE: bug fix, small feature, config change, docs update, style fix, test addition.
COMPLEX: architecture change, multi-file refactor, security audit, system design,
migration, performance optimization, new subsystem, cross-cutting concern.

Issue title: {title}
Issue body: {body}

Reply with exactly one word: SIMPLE or COMPLEX
```

### 4. Add triage event logging

Log a new `triage` event so the dashboard can show which model was selected and why.

### 5. Fallback

If the triage call fails (timeout, parse error), default to Sonnet.

## Acceptance Criteria

- [ ] Simple issues (bug fix, docs) dispatch to `claude-sonnet-4-6`
- [ ] Complex issues (refactor, architecture) dispatch to `claude-opus-4-6`
- [ ] Triage takes <10 seconds
- [ ] Events show correct model after triage
- [ ] Triage failure defaults to Sonnet gracefully
- [ ] No model-specific labels needed — single `agent` label only
