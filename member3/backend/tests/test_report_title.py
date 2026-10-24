from startupforge_member3.reports import _report_title


def test_report_title_keeps_a_short_project_name():
    assert _report_title("Campus Meals") == "Campus Meals — Startup Feasibility Report"


def test_report_title_truncates_a_long_project_name_to_the_schema_limit():
    title = _report_title("An app for housing societies in Pune that lets residents reserve shared EV charging points, "
                          "schedule charging sessions around vehicle needs, and split electricity costs transparently.")

    assert len(title) <= 200
    assert title.endswith(" — Startup Feasibility Report")
    assert title.endswith("… — Startup Feasibility Report")


def test_report_title_handles_a_long_unbroken_name():
    title = _report_title("x" * 200)

    assert len(title) <= 200
    assert title.endswith("… — Startup Feasibility Report")
