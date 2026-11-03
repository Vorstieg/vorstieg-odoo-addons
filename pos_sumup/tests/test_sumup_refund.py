from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user


class TestPosSumupRefund(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.provider = cls.env['payment.provider'].search([
            ('code', '=', 'sumup'),
            ('company_id', '=', cls.company.id),
        ], limit=1)
        if not cls.provider:
            cls.provider = cls.env['payment.provider'].create({
                'name': 'SumUp',
                'code': 'sumup',
                'state': 'enabled',
                'company_id': cls.company.id,
                'sumup_merchant_code': 'merchant-code',
                'sumup_api_key': 'test-api-key',
            })
        else:
            cls.provider.write({
                'state': 'enabled',
                'sumup_merchant_code': 'merchant-code',
                'sumup_api_key': 'test-api-key',
            })

        journal = cls.env['account.journal'].search([
            ('company_id', '=', cls.company.id),
            ('type', '=', 'bank'),
        ], limit=1)
        cls.reader = cls.env['sumup.terminal'].create({
            'name': 'Test Reader',
            'terminal_id': 'rdr_test',
        })
        cls.payment_method = cls.env['pos.payment.method'].create({
            'name': 'SumUp Terminal',
            'journal_id': journal.id,
            'payment_method_type': 'terminal',
            'use_payment_terminal': 'sumup',
            'reader': cls.reader.id,
        })

    def test_sumup_full_refund_request(self):
        responses = [
            {'id': 'txn-1', 'status': 'SUCCESSFUL', 'amount': 20.0},
            {},
        ]
        with patch.object(type(self.provider), 'sumup_make_request', side_effect=responses) as mocked:
            result = self.payment_method.sumup_make_refund_request({
                'transaction_id': 'txn-1',
                'amount': 20.0,
            })

        self.assertEqual(result['success'], True)
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(mocked.call_args_list[1].kwargs['data'], {})

    def test_sumup_partial_refund_request(self):
        with (
            patch.object(type(self.payment_method), '_get_sumup_refunded_amount', return_value=5.0),
            patch.object(type(self.provider), 'sumup_make_request', side_effect=[
                {'id': 'txn-2', 'status': 'SUCCESSFUL', 'amount': 20.0},
                {},
            ]) as mocked,
        ):
            result = self.payment_method.sumup_make_refund_request({
                'transaction_id': 'txn-2',
                'amount': 10.0,
            })

        self.assertEqual(result['success'], True)
        self.assertEqual(mocked.call_args_list[1].kwargs['data'], {'amount': 10.0})

    def test_sumup_refund_amount_cannot_exceed_remaining_amount(self):
        with (
            patch.object(type(self.payment_method), '_get_sumup_refunded_amount', return_value=19.0),
            patch.object(type(self.provider), 'sumup_make_request', return_value={
                'id': 'txn-3',
                'status': 'SUCCESSFUL',
                'amount': 20.0,
            }) as mocked,
        ):
            result = self.payment_method.sumup_make_refund_request({
                'transaction_id': 'txn-3',
                'amount': 2.0,
            })

        self.assertEqual(result['error']['code'], 'REFUND_LIMIT_EXCEEDED')
        self.assertEqual(mocked.call_count, 1)

    def test_poll_status_maps_404_to_pending(self):
        """A 404 while polling means the transaction is not indexed yet and must
        be reported as PENDING so the front-end keeps polling instead of
        treating it as a hard error."""
        with patch.object(type(self.provider), 'sumup_make_request', return_value={
            'error': {'code': 'API_ERROR', 'message': 'Not Found', 'http_status': 404},
        }):
            result = self.payment_method.proxy_sumup_request(
                {'client_transaction_id': 'ctid-1'}, 'poll_status'
            )

        self.assertEqual(result, {'status': 'PENDING'})

    def test_poll_status_passes_through_real_errors(self):
        """Non-404 errors (e.g. auth) must not be hidden behind PENDING."""
        error = {'error': {'code': 'AUTH_ERROR', 'message': 'nope', 'http_status': 401}}
        with patch.object(type(self.provider), 'sumup_make_request', return_value=error):
            result = self.payment_method.proxy_sumup_request(
                {'client_transaction_id': 'ctid-2'}, 'poll_status'
            )

        self.assertEqual(result, error)

    def test_poll_status_returns_successful_transaction(self):
        """A resolved transaction is returned untouched to the front-end."""
        transaction = {'id': 'txn-9', 'status': 'SUCCESSFUL', 'amount': 42.0}
        with patch.object(type(self.provider), 'sumup_make_request', return_value=transaction):
            result = self.payment_method.proxy_sumup_request(
                {'client_transaction_id': 'ctid-3'}, 'poll_status'
            )

        self.assertEqual(result, transaction)

    def test_sumup_refund_falls_back_to_transaction_code_lookup(self):
        with patch.object(type(self.provider), 'sumup_make_request', side_effect=[
            {},
            {'id': 'txn-4', 'status': 'SUCCESSFUL', 'amount': 20.0},
            {},
        ]) as mocked:
            result = self.payment_method.sumup_make_refund_request({
                'transaction_id': 'legacy-transaction-code',
                'amount': 20.0,
            })

        self.assertEqual(result['success'], True)
        self.assertEqual(result['transaction_id'], 'txn-4')
        self.assertEqual(mocked.call_args_list[0].kwargs['params'], {'id': 'legacy-transaction-code'})
        self.assertEqual(
            mocked.call_args_list[1].kwargs['params'],
            {'transaction_code': 'legacy-transaction-code'},
        )

    def test_pos_user_can_start_payment(self):
        """A cashier with only 'Point of Sale / User' has no access to
        payment.provider but must still be able to send a payment to the reader."""
        pos_user = new_test_user(self.env, 'sumup_cashier', groups='base.group_user,point_of_sale.group_pos_user')
        with self.assertRaises(AccessError):
            self.env['payment.provider'].with_user(pos_user).search([('code', '=', 'sumup')])

        with patch.object(type(self.provider), 'sumup_make_request', return_value={
            'data': {'client_transaction_id': 'ctid-1'},
        }) as mocked:
            result = self.payment_method.with_user(pos_user).proxy_sumup_request({
                'total_amount': {'value': 1000, 'currency': 'EUR', 'minor_unit': 2},
            }, 'payment')

        self.assertEqual(result, {'data': {'client_transaction_id': 'ctid-1'}})
        self.assertEqual(
            mocked.call_args.args[0],
            'v0.1/merchants/merchant-code/readers/rdr_test/checkout',
        )

    def test_pos_user_can_request_refund(self):
        pos_user = new_test_user(self.env, 'sumup_cashier', groups='base.group_user,point_of_sale.group_pos_user')
        with patch.object(type(self.provider), 'sumup_make_request', side_effect=[
            {'id': 'txn-5', 'status': 'SUCCESSFUL', 'amount': 20.0},
            {},
        ]):
            result = self.payment_method.with_user(pos_user).sumup_make_refund_request({
                'transaction_id': 'txn-5',
                'amount': 20.0,
            })

        self.assertEqual(result['success'], True)

    def test_non_pos_user_cannot_send_request(self):
        user = new_test_user(self.env, 'sumup_employee', groups='base.group_user')
        with patch.object(type(self.provider), 'sumup_make_request') as mocked:
            with self.assertRaises(AccessError):
                self.payment_method.with_user(user).proxy_sumup_request({
                    'total_amount': {'value': 1000, 'currency': 'EUR', 'minor_unit': 2},
                }, 'payment')

        mocked.assert_not_called()
