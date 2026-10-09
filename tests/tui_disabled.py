"""The TUI tests that are disabled (#777).

The owner's standing rule of 2026-10-10: the TUI will be deleted, so every
test that imports ``textual``, ``kstrl.tui`` or the TUI helpers is disabled.
``tests/conftest.py`` reads this module. A whole file goes in
``IGNORED_FILES`` and is not collected. A test in a file that also holds
other tests goes in ``SKIPPED_TESTS`` and shows as a skip with ``REASON``.
To enable a test again, delete its entry here.
"""

from __future__ import annotations

REASON = "TUI disabled (#777): the TUI will be deleted"

IGNORED_FILES: tuple[str, ...] = (
    "test_config_screen.py",
    "test_evolve_screen.py",
    "test_evolve_screen_repairs.py",
    "test_home_data.py",
    "test_launch_session.py",
    "test_retry_screen.py",
    "test_settle_helper.py",
    "test_tui_433_inc3.py",
    "test_tui_433_inc3_board.py",
    "test_tui_433_inc4.py",
    "test_tui_433_inc5.py",
    "test_tui_433_inc6.py",
    "test_tui_433_screens.py",
    "test_tui_433_verify.py",
    "test_tui_app.py",
    "test_tui_detail.py",
    "test_tui_runs.py",
    "test_tui_safe_mode.py",
    "test_tui_snapshots.py",
    "test_tui_tail.py",
)

SKIPPED_TESTS: dict[str, tuple[str, ...]] = {
    "test_acceptance_gate_e2e.py": (
        "test_a_held_out_failure_halts_and_an_approval_merges_over_it",
    ),
    "test_carried_component_state.py": (
        "TestTheRunRecordsWhatItCarried::test_the_dashboard_renders_carried_components",
        "TestTheRunRecordsWhatItCarried::test_the_run_list_counts_only_what_the_run_did",
        "TestARunThatFinishesACarriedComponent::test_a_merge_the_run_confirms_counts_as_done",
    ),
    "test_config_guard_survey.py": (
        "test_a_launched_run_reports_the_array_instead_of_raising",
        "test_the_init_wizard_reports_the_array_instead_of_raising",
        "test_the_env_scrub_predicate_sees_both_launched_run_handles",
        "test_the_config_screen_refresh_reports_the_seam_wording",
    ),
    "test_config_preflight.py": (
        "TestTheHomeShellIsNotAFifthExemption::test_a_rejected_section_stops_the_shell_before_it_opens",
        "TestTheHomeShellIsNotAFifthExemption::test_a_usable_config_still_opens_the_shell",
    ),
    "test_decompose_screens.py": (
        "TestComputeTiers::test_chain_and_diamond",
        "TestComputeTiers::test_unknown_deps_ignored",
        "TestComputeTiers::test_cycle_marks_members_not_raises",
        "TestComputeTiers::test_self_dependency_is_a_cycle",
        "TestArchitectComponentId::test_a_pre_281_decompose_dir_resolves_to_the_bare_key",
        "TestArchitectComponentId::test_a_post_281_decompose_dir_resolves_to_the_namespaced_key",
        "TestArchitectComponentId::test_a_new_dir_with_a_component_named_architect_never_falls_back",
        "TestArchitectComponentId::test_a_factory_run_never_falls_back",
        "TestDispatch::test_kinds_map_to_stacks",
        "TestDecomposeScreen::test_success_run_renders_dag_and_summary",
        "TestDecomposeScreen::test_a_component_named_architect_is_not_filtered_out_of_the_dag",
        "TestDecomposeScreen::test_a_pre_281_run_dir_still_renders_as_the_run_it_was",
        "TestDecomposeScreen::test_a_pre_281_halted_run_still_shows_its_blocker_banner",
        "TestDecomposeScreen::test_state_update_after_header_removed_is_safe",
        "TestDecomposeScreen::test_triage_shows_blocker_banner_and_detail",
        "TestDecomposeScreen::test_escape_pops_to_overview",
        "TestStatusTui::test_explicit_tui_opens_the_newest_run_with_kind_dispatch",
        "TestStatusTui::test_non_tty_default_stays_plain",
    ),
    "test_deeply_nested_json.py": (
        "test_a_deeply_nested_event_line_is_skipped_and_the_tail_continues",
    ),
    "test_evolve_screen_encoding.py": ("test_the_evolve_screen_survives_both_undecodable_files",),
    "test_feature_run.py": ("TestFeatureEmbeddedGate::test_gate_opens_options_modal_and_unblocks",),
    "test_home_shell.py": (
        "TestHomeScreen::test_renders_runs_and_identity",
        "TestHomeScreen::test_missing_toml_warns_in_masthead",
        "TestHomeScreen::test_enter_opens_run_with_kind_dispatch_and_escape_returns",
        "TestHomeScreen::test_q_over_a_run_pops_home_not_exit",
        "TestHomeScreen::test_dash_command_opens_newest_run",
        "TestHomeScreen::test_a_focus_in_flight_when_the_first_queue_read_lands_keeps_it",
        "TestHomeScreen::test_digit_hotkey_opens_matching_command",
        "TestHomeScreen::test_preview_tracks_highlight_and_enter_opens_that_run",
        "TestHomeScreen::test_empty_state_renders_guidance",
        "TestDashUnchanged::test_standalone_dash_q_still_detaches",
    ),
    "test_inbox.py": (
        "TestInboxScreen::test_screen_imports_and_registers",
        "TestInboxScreen::test_screen_renders_and_approves",
    ),
    "test_inbox_waivers.py": ("test_the_inbox_screen_says_what_approve_and_reject_do",),
    "test_init_wizard.py": (
        "TestWizardScreen::test_detected_line_renders_the_stack_checks",
        "TestWizardScreen::test_no_stack_shows_that_nothing_runs",
        "TestWizardScreen::test_a_malformed_kstrl_toml_does_not_take_the_app_down",
        "TestWizardScreen::test_a_malformed_verify_section_still_shows_the_unreadable_row",
        "TestWizardScreen::test_a_retired_key_beside_a_confirmed_stack_shows_the_unreadable_row",
        "TestWizardScreen::test_a_malformed_factory_section_shows_the_unreadable_row",
        "TestWizardScreen::test_happy_path_scaffolds_and_writes_agent",
        "TestWizardScreen::test_preview_flags_a_stale_prompt_instead_of_kept",
        "TestWizardScreen::test_existing_toml_keeps_agent_settings_out",
        "TestWizardScreen::test_bad_directory_blocks_preview",
        "TestWizardScreen::test_file_target_blocks_preview",
        "TestWizardScreen::test_worker_error_is_terminal_and_navigation_waits",
    ),
    "test_merge_has_one_recorder.py": (
        "TestARepolledMergeIsInTheStream::test_both_paths_record_the_same_merge_in_the_manifest_and_the_stream",
        "TestARepolledMergeIsInTheStream::test_a_parked_pr_known_only_by_its_url_is_recorded_by_its_number",
    ),
    "test_runid.py": (
        "TestMixedKindDiscovery::test_newest_first_across_kinds",
        "TestMixedKindDiscovery::test_kinds_filter",
        "TestMixedKindDiscovery::test_held_lock_attributed_to_newest_factory_run",
    ),
    "test_shutdown.py": ("TestWorkerSigterm::test_pool_worker_sigterm_kills_agent_group",),
    "test_tui_433_verify552.py": (
        "TestIntegrationJoinIsPerFeature::test_another_features_state_file_is_not_this_runs_disposition",
        "TestIntegrationJoinIsPerFeature::test_an_id_this_run_opened_but_another_run_opened_in_state_is_unknown",
        "TestServeItemAfterTheDaemonDied::test_a_stranded_item_is_not_in_flight",
        "TestServeItemAfterTheDaemonDied::test_home_does_not_list_it_as_running",
        "TestPlantsTheSuiteMissed::test_a_disabled_inbox_is_not_counted_as_nothing_waiting",
        "TestPlantsTheSuiteMissed::test_a_status_kstrl_does_not_write_is_unknown_not_open",
    ),
    "test_tui_config_guard.py": (
        "TestEvolveScreen::test_a_bad_knob_is_named_instead_of_raised",
        "TestEvolveScreen::test_the_tables_are_empty_and_the_emptiness_is_explained",
        "TestEvolveScreen::test_a_malformed_document_is_named_too",
        "TestEvolveScreen::test_the_environment_variable_is_named_when_it_is_the_cause",
        "TestEvolveScreen::test_the_banner_is_hidden_on_a_config_that_resolves",
        "TestEvolveScreen::test_reload_clears_the_banner_once_the_file_is_repaired",
        "TestInboxScreen::test_a_malformed_document_is_named_instead_of_raised",
        "TestInboxScreen::test_it_never_claims_the_inbox_is_clear",
        "TestInboxScreen::test_a_decision_taken_after_the_file_broke_redraws",
        "TestSharedGuard::test_env_scrub_is_safe_reads_the_launched_session",
    ),
    "test_tui_embed.py": (
        "TestEmbeddedApp::test_checkpoint_modal_answers_the_channel",
        "TestEmbeddedApp::test_quit_flow_requests_graceful_stop",
        "TestEmbeddedApp::test_quit_declined_keeps_running",
        "TestEmbeddedApp::test_pending_checkpoint_reopens_with_c",
        "TestEmbeddedApp::test_generic_prompt_uses_request_labels_and_valid_choices",
        "TestScreenFactory::test_custom_stack_pushed_bottom_first",
        "TestOptionsModal::test_confirm_resolves_through_the_channel",
        "TestOptionsModal::test_escape_leaves_pending_and_c_reopens",
        "TestRunFactoryEmbeddedHandsTheCallersLockIn::test_the_worker_finds_the_callers_lock_still_held",
    ),
    "test_unanswered_gate_parks.py": (
        "TestAnEmbeddedRunParksWhenTheTuiGoesAway::test_the_gate_waiting_when_the_tui_goes_parks",
    ),
    "test_understand_run.py": (
        "TestUnderstandDashboard::test_component_screen_renders_understand_run",
    ),
}
