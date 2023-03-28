import sys
import unittest

sys.path.append("../sso-elevator")

import slack


class SlackHelpersTest(unittest.TestCase):
    def test_string(self):
        account_id_in = "1234567890"
        message = slack.prepare_approval_request(
            channel="channel",
            requester_slack_id="requester_slack_id",
            account_id=account_id_in,
            requires_approval=False,
            role_name="role_name",
            reason="reason",
        )
        account_id_out = slack.find_value_in_content_block(message["blocks"], "AccountId")
        self.assertEqual(account_id_in, account_id_out)


if __name__ == "__main__":
    unittest.main()
