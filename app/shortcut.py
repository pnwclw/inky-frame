"""Generate an (unsigned) iOS Shortcut that posts a photo to this service.

The shortcut is one "Choose from Menu" with four leaves; each leaf is a "Get
Contents of URL" with a literal URL whose query string carries the chosen
orientation + fit. Using four literal URLs (instead of variables interpolated
into a URL) keeps the plist simple and robust — no text-token attachments.

It is a plain XML plist (`.shortcut`). Apple-signed shortcuts are AEA archives;
we can't sign offline, so importing this requires Settings → Shortcuts → "Allow
Untrusted Shortcuts" (a.k.a. Private Sharing). See the landing page for steps.

Format reference: the Shortcuts workflow plist schema (WFWorkflowActions, the
choosefrommenu control-flow modes 0/1/2, downloadurl parameters).
"""

from __future__ import annotations

import plistlib

# (menu label, orientation, fit) — fit maps to the API's cover/contain.
MENU_OPTIONS: list[tuple[str, str, str]] = [
    ("Landscape · Cover (fill)", "landscape", "cover"),
    ("Landscape · Fit (center)", "landscape", "contain"),
    ("Portrait · Cover (fill)", "portrait", "cover"),
    ("Portrait · Fit (center)", "portrait", "contain"),
]

# Stable UUIDs so re-imports replace rather than duplicate.
_SELECT_UUID = "1B7F1C00-0000-4000-8000-000000000001"
_MENU_UUID = "1B7F1C00-0000-4000-8000-000000000002"


def _select_photos_action() -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.selectphoto",
        "WFWorkflowActionParameters": {
            "WFSelectMultiplePhotos": False,
            "UUID": _SELECT_UUID,
        },
    }


def _menu_start_action() -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.choosefrommenu",
        "WFWorkflowActionParameters": {
            "GroupingIdentifier": _MENU_UUID,
            "WFControlFlowMode": 0,
            "WFMenuPrompt": "How should it be shown?",
            "WFMenuItems": [label for label, _, _ in MENU_OPTIONS],
        },
    }


def _menu_case_action(label: str) -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.choosefrommenu",
        "WFWorkflowActionParameters": {
            "GroupingIdentifier": _MENU_UUID,
            "WFControlFlowMode": 1,
            "WFMenuItemTitle": label,
        },
    }


def _menu_end_action() -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.choosefrommenu",
        "WFWorkflowActionParameters": {
            "GroupingIdentifier": _MENU_UUID,
            "WFControlFlowMode": 2,
        },
    }


def _post_image_action(image_url: str, orientation: str, fit: str) -> dict:
    url = f"{image_url}?orientation={orientation}&fit={fit}"
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
        "WFWorkflowActionParameters": {
            "WFURL": url,
            "WFHTTPMethod": "POST",
            "WFHTTPBodyType": "File",
            # Body = the photo chosen by the Select Photos action above.
            "WFRequestVariable": {
                "WFSerializationType": "WFTextTokenAttachment",
                "Value": {
                    "Type": "ActionOutput",
                    "OutputUUID": _SELECT_UUID,
                    "OutputName": "Photos",
                },
            },
            "ShowHeaders": False,
        },
    }


def build_shortcut_plist(image_url: str) -> bytes:
    """Build the shortcut. `image_url` is the full POST endpoint, e.g.
    http://inky-frame.local:8080/display/image."""
    actions: list[dict] = [_select_photos_action(), _menu_start_action()]
    for label, orientation, fit in MENU_OPTIONS:
        actions.append(_menu_case_action(label))
        actions.append(_post_image_action(image_url, orientation, fit))
    actions.append(_menu_end_action())

    workflow = {
        "WFWorkflowClientVersion": "1146.13",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {
            "WFWorkflowIconStartColor": 946986751,  # teal
            "WFWorkflowIconGlyphNumber": 59511,      # picture glyph
        },
        "WFWorkflowImportQuestions": [],
        "WFWorkflowTypes": ["ActionExtension"],          # appears in the Share Sheet
        "WFWorkflowInputContentItemClasses": ["WFImageContentItem"],
        "WFWorkflowActions": actions,
    }
    return plistlib.dumps(workflow, fmt=plistlib.FMT_XML)
