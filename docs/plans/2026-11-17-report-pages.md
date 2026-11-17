# Offline report pages implementation plan

Goal: replace the long image wall with a compact overview and one detail page per scene, for both golden galleries and regression reports.

Approved design: overview statistics, scene search, algorithm/status filters and all matching scenes shown together (pagination removed by the user follow-up). Regression defaults to abnormal scenes when present. Each scene has algorithm tabs, baseline/candidate comparison, optional difference, one selected ROI, metrics with limits, timing and collapsed evidence. Keep file:// opening, relative links and the existing PNG files. Preserve rendering recipes, thresholds and results. Add ordinary commits; never amend or force-push for this task.

Architecture: scripts/report.py renders the overview and scene pages; scripts/report_assets/ supplies local CSS and JavaScript copied into each report. write_report keeps the current JSON/CSV output contract. generate_gallery uses the same page writer with golden images in place. A regeneration CLI rewrites presentation only from saved results.

Constraints: no engine runs/builds, no dependency installs, no image copies or crops for pagination, no fetch/server/CDN, no upload of local configs or .workspace. Work in the existing clean CoronaTests task branch; leave CoronaEngine and its current user edits alone.

1. Add failing tests for compact overviews, scene pages/navigation, asset containment and offline links, preserving raw files and exact result statuses. Adapt existing detail assertions to read the scene page.
2. Implement shared rendering and local browser controls. Validate search/status/algorithm composition, displaying every matching row, algorithm selection and one-ROI display. Keep missing/failed/unsupported states explicit.
3. Integrate golden generation, capture-time threshold display data and presentation-only regeneration. Existing sealed golden bundles must not be rewritten in place.
4. Regenerate the repository gallery and the user's completed regression page. Check every local link, all 92 golden SHA256 values, both local configs and saved run JSON/CSV/captures. Run the complete CPU test suite and review the diff.
5. Commit normally and push main without rewriting history. Verify the remote head and parent.

Review focus: mixed statuses in a single scene; no abnormal scenes; subsets with absent algorithms; nested page URLs and escaping; preservation of sealed metadata and original image bytes. Tests must cover these cases.
