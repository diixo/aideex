

TRACKER_ICONS: dict[int, str] = {
    2:   "bug_report",              # Bugs
    3:   "published_with_changes",  # Change Requests — "changes" arrow cycle
    5:   "lightbulb",               # Requirement Specifications (CRS / SRS)
    6:   "check_circle",            # Tasks
    8:   "schedule",                # Timekeeping — clock face
    9:   "play_arrow",              # Test Runs — triangle play button
    10:  "cloud",                   # User Stories
    11:  "warning",                 # Risks — flame/explosion (no `bomb` in classic Material Icons)
    102: "outlined_flag",           # Test Cases — test tube
    103: "directions_subway",       # Releases — launch icon
    107: "phone",                   # Contacts — handset
    108: "playlist_play",           # Test Sets — collection of tests
    109: "tune",                    # Test Configurations — sliders
    150: "groups",                  # Teams — multiple people
    12:  "e_mobiledata",            # Epic
    13:  "description",             # Document
    1:   "fireplace",               # Issue
    105: "extension",               # Components — puzzle piece
}

STATUS_COLORS = {
    "New":      "#dc3545",
    "Verified": "#fd7e14",
    "Reopened": "#fd7e14",
    "Resolved": "#198754",
    "Closed":   "#0f5132",
    "Rejected": "#6c757d",
    "Draft":    "#ffab46",
}

PRIORITY_COLORS = {
    "Highest": "#dc3545",
    "High": "#fd7e14",
    "Normal": "#198754",
    "Low": "#0d6efd",
    "Lowest": "#6c757d",
}
