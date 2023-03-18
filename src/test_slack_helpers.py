import unittest

from slack_helpers import find_value_in_content_block, prepare_slack_approval_request


class SlackHelpersTest(unittest.TestCase):
    def test_string(self):
        account_id_in = "1234567890"
        message = prepare_slack_approval_request(
            "channel", "requester_slack_id", account_id_in, "role_name", "reason"
        )
        account_id_out = find_value_in_content_block(message["blocks"], "AccountId")
        self.assertEqual(account_id_in, account_id_out)


if __name__ == "__main__":
    unittest.main()
