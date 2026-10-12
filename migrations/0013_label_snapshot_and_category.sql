-- P7 F11 + F12 (P7-EVAL-SET-BRIEF.md §2). Labelling starts at the pilot's first call (2026-10-18)
-- and neither change can be applied retroactively: both capture something at label time that is
-- destroyed afterwards.
--
-- F11 -- pin the labelled turn's text onto the label. `turns.user_text` / `answer_text` are purged
-- after ORCA_METERING_TURN_RETENTION_DAYS (30); 0004 deliberately gave call_labels NO foreign key
-- to turns so a label would outlive that purge -- but a label that outlives its text is a verdict
-- about nothing. So add_label now copies (user_text, answer_text, tools) from the turn row, and the
-- call's channel, in the same transaction as the insert. The purge sweep is UNCHANGED: turns still
-- clears at 30 days; the label keeps its own copy. Still no FK to turns (0004's intent stands).
--
--   * depth IS NULL is a legal label (0004: "null = the whole call"). There is no single turn to
--     snapshot, so the four columns stay null. A whole-call verdict was never going to become a
--     runner case; the F14 export skips them.
--   * If the turn row is missing or already purged when the label is written, the label STILL
--     WRITES, with a null snapshot. Labelling never fails because the text is gone.
--   * `channel` (voice | chat) is copied from calls because turns has no channel column; F13
--     (max_ttft_ms) needs it on the label.
--   * Legacy rows keep all four columns null: their text is not recoverable.
--
-- RETENTION CONSEQUENCE, by design: a labelled turn's text (PII: whatever the caller said and the
-- agent answered) now persists past 30 days, for as long as the label does. Labels are a small,
-- hand-picked set; this is NOT a general retention change. It belongs in the O5 write-up.
--
-- F12 -- a failure category from a CLOSED list, enforced here and not in the app (same precedent
-- as agents.class in 0010). Two entries are REVIEW-ONLY on purpose: `wrong_data` and `tone` cannot
-- be asserted from a turn_summary line and must never be faked into a runner case. Mapping at F14:
--   wrong_tool     -> tool_calls_not_containing / max_tool_calls_by_name
--   no_answer      -> tool_calls_not_containing:[search_knowledge] + review
--   wrong_language -> reply_language
--   too_slow       -> max_ttft_ms (F13)
--   loop           -> max_llm_iterations + missing_details_ask
--   wrong_data     -> REVIEW-ONLY
--   tone           -> REVIEW-ONLY
-- `category` is null for good/unsure and REQUIRED for bad. That second rule is added NOT VALID:
-- it is enforced on every insert and update from now on, but rows labelled 'bad' before this
-- migration (no category exists for them, and none is invented) are not rejected by it.
alter table orca_gw.call_labels
    add column user_text   text,
    add column answer_text text,
    add column tools       jsonb,     -- [{name,status}] as recorded on the turn
    add column channel     text,      -- voice | chat
    add column category    text;

alter table orca_gw.call_labels
    add constraint call_labels_category_check
        check (category is null or category in
            ('wrong_tool', 'no_answer', 'wrong_language', 'too_slow', 'loop',
             'wrong_data', 'tone')),
    add constraint call_labels_bad_needs_category
        check (verdict <> 'bad' or category is not null) not valid;
