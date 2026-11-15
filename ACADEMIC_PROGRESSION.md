# Cohort academic progression

## Data model

- `cohorts` stores a stable intake identity and optional admission year / academic-year start date.
- `cohort_prefix_mappings` contains administrator-configured registration prefixes. New student entries use the longest active matching prefix after format validation; unknown formats remain pending.
- `academic_periods` stores each cohort's semester number, label, and inclusive ISO date range. Dates must be supplied by an administrator; no semester duration or university date is assumed.
- `event_cohorts` maps one event ID to each participating cohort and snapshots the period applicable on the event date. Events may involve selected cohorts or all active cohorts.
- `event_participants.academic_period_id` and `attendance.academic_period_id` preserve the period associated with each volunteer's participation and attendance.

The global `events.semester` column remains for legacy records. New events use cohort mappings. Existing attendance rows are not backfilled or reassigned by the migration; their legacy event semester remains the fallback. When a configured period is edited, confirmed snapshots remain attached to their period record. A deliberate event reschedule recalculates affected mappings and attendance snapshots and writes the before/after values to the audit log. Cancelled events cannot be rescheduled.

## Admin workflow

1. Open **Academic calendars** and create each intake with its admission year and optional academic-year start date.
2. Configure that intake's validated registration prefix. Prefix mappings are used only for new records; existing volunteers can be reviewed and assigned manually.
3. Add each semester's inclusive start and end dates. Periods cannot overlap within a cohort. The current semester is calculated from today's date; the current academic year is derived from the semester sequence (semesters 1–2 are year 1, 3–4 are year 2, and so on).
4. Create an event once and select cohorts or all active cohorts. Each cohort mapping displays its applicable semester or stays pending if the event date is outside configured periods.
5. Record attendance normally. Its academic period is copied from the participant snapshot. Pending mappings never receive a guessed period.

If an event is upcoming and its cohort mapping is pending, configuring a period that covers its date resolves the pending mapping only when the cohort has no attendance recorded for that event. Historical or attended event mappings require an explicit reschedule to recalculate and audit snapshots.

## Migration and verification

`init_db()` performs additive, repeatable schema updates at application startup. Legacy cohort labels are registered and linked by exact label; registration numbers are not used to rewrite old cohort assignments. Existing data is not reset. Back up the database before deployment and verify the cohort review queue before relying on cohort reports.

The integration test `test_one_event_maps_to_distinct_cohort_periods_and_keeps_attendance_snapshot` covers one shared event mapping two cohorts to different semesters, attendance snapshots, and an event date outside configured periods. Run the test suite with `py -3 -m unittest discover -s tests -v` in an environment with the project requirements installed. This workspace's current environment does not have a Python interpreter, so the test and migration have not been executed here.

## Current scope

This increment adds date-based cohort period assignment and preserves the existing administrative login gate. It does not yet add distinct faculty / cohort / event coordinator roles, cohort-specific credit rules, an incoming-leader appointment workflow, or automated backup and restore. Those controls require separate design and verification before production use.
