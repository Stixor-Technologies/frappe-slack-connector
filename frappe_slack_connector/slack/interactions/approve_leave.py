import frappe
from frappe import _
from frappe.utils import get_url_to_form

from frappe_slack_connector.db.leave_application import (
    approve_leave,
    custom_fields_exist,
    reject_leave,
)
from frappe_slack_connector.db.user_meta import get_userid_from_slackid
from frappe_slack_connector.helpers.error import generate_error_log
from frappe_slack_connector.helpers.standard_date import standard_date_fmt
from frappe_slack_connector.helpers.str_utils import strip_html_tags
from frappe_slack_connector.override.leave_application import format_leave_status_blocks
from frappe_slack_connector.slack.app import SlackIntegration


def handler(slack: SlackIntegration, payload: dict):
    """
    Handle the interaction when a leave application is approved or rejected
    Update the message in Slack with the status of the leave application
    Update the leave application in ERP accordingly
    """
    try:
        # Check the user who sent the request
        user_id = payload.get("user", {}).get("id")
        if not user_id:
            generate_error_log("User ID not found in payload", msgprint=True)
        frappe.set_user(get_userid_from_slackid(user_id))

        action_id = payload["actions"][0]["action_id"]
        leave_id = payload["actions"][0]["value"]

        # Process the action based on action_id
        if action_id == "leave_approve":
            approve_leave(leave_id)
        elif action_id == "leave_reject":
            reject_leave(leave_id)
        else:
            frappe.throw(_("Unknown action"))

        blocks = payload["message"]["blocks"]

        status_text = "Approved :white_check_mark:" if action_id == "leave_approve" else "Rejected :x:"

        # Replace the actions block with a status update
        for i, block in enumerate(blocks):
            if block.get("block_id") == "leave_actions_block":
                blocks[i] = {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Status:* {status_text}",
                    },
                }
            elif block.get("block_id") == "footer_block":
                # delete the footer block
                blocks.pop(i)

        # Update the message with the new blocks
        slack.slack_app.client.chat_update(
            channel=payload["channel"]["id"],
            ts=payload["container"]["message_ts"],
            blocks=blocks,
        )

        # Notify the applicant about the leave decision
        try:
            leave_doc = frappe.get_doc("Leave Application", leave_id)
            applicant_slack_id = slack.get_slack_user_id(employee_id=leave_doc.employee)
            if applicant_slack_id:
                duration = "Full Day"
                if leave_doc.half_day:
                    duration = (
                        leave_doc.custom_first_halfsecond_half
                        if custom_fields_exist()
                        else "Half Day"
                    )
                slack.slack_app.client.chat_postMessage(
                    channel=applicant_slack_id,
                    blocks=format_leave_status_blocks(
                        leave_id=leave_id,
                        leave_link=get_url_to_form("Leave Application", leave_id),
                        leave_type=leave_doc.leave_type,
                        from_date=standard_date_fmt(leave_doc.from_date),
                        to_date=standard_date_fmt(leave_doc.to_date),
                        duration=duration,
                        is_approved=action_id == "leave_approve",
                    ),
                )
        except Exception as notify_err:
            generate_error_log(
                title="Error notifying applicant about leave decision",
                exception=notify_err,
            )
    except Exception as e:
        # show an error modal with the exception message
        slack.slack_app.client.views_open(
            trigger_id=payload["trigger_id"],
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": "Error"},
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": ":warning: Error taking action on leave request",
                            "emoji": True,
                        },
                    },
                    {"type": "divider"},
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Error Details:*\n```{strip_html_tags(str(e))}```",
                        },
                    },
                ],
            },
        )
