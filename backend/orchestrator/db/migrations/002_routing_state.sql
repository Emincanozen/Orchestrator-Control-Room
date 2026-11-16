-- M2b: persisted routing/fallback state.
-- routing_decisions.detail  : classification + full router decision (rejections, tiers, flags).
-- tasks.route_exclusions    : JSON list of {provider, model|null, reason} the router must skip for this task
--                             (cumulative fallback exclusions).
-- tasks.provider_switches   : how many fallback provider switches this task has used.
-- tasks.granted_permission  : JSON {permission, phase, approval_id} recorded when a permission_escalation
--                             approval is approved (NULL = nothing granted).
ALTER TABLE routing_decisions ADD COLUMN detail TEXT NOT NULL DEFAULT '{}';
ALTER TABLE tasks ADD COLUMN route_exclusions TEXT NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN provider_switches INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN granted_permission TEXT;
