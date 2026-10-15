-- Rate variants of one programme / level (marginal, reduced, per-course, thesis ...);
-- NULL is the standard rate. Part of the TUITION key (tuition_id) when set.
ALTER TABLE TUITION ADD COLUMN rate_label TEXT;
