# coding: utf-8
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
from odoo import fields, models, _, api
from odoo.exceptions import UserError, AccessError

_logger = logging.getLogger(__name__)

class PosPaymentMethod(models.Model):
    _inherit = 'pos.payment.method'

    def _get_payment_terminal_selection(self):
        return super(PosPaymentMethod, self)._get_payment_terminal_selection() + [('sumup', 'Sumup')]

    reader = fields.Many2one('sumup.terminal', string='Sumup Solo')
    sumup_tipping_enabled = fields.Boolean(string='Enable Tipping')
    sumup_tip_rates = fields.Char(string='Tip Rates (%)', help='Comma-separated list of percentages, e.g., "10,15,20"')

    @api.model
    def _load_pos_data_fields(self, config):
        params = super()._load_pos_data_fields(config)
        params += ['reader']
        return params

    def _get_sumup_payment_provider(self):
        provider = self.env['payment.provider'].search([
            ('code', '=', 'sumup'),
            ('company_id', '=', self.env.company.id)
        ], limit=1)
        return provider

    def _get_sumup_refunded_amount(self, transaction_id):
        domain = [
            ('payment_method_id', '=', self.id),
            ('transaction_id', '=', transaction_id),
            ('amount', '<', 0),
            ('payment_status', '!=', 'cancelled'),
            ('pos_order_id.state', '!=', 'cancel'),
        ]
        refunded_amount = sum(abs(amount) for amount in self.env['pos.payment'].search(domain).mapped('amount'))
        return refunded_amount

    def proxy_sumup_request(self, data, operation=False):
        ''' Handles the request to the SumUp API.
        :param data: The data to be sent to SumUp.
        :param operation: The operation to be performed (payment, poll_status).
        '''
        self.ensure_one()
        provider = self._get_sumup_payment_provider()
        if not provider:
            return False

        if not self.reader:
            return {'error': {'code': 'CONFIG_ERROR', 'message': _("Please select a SumUp reader for this payment method.")}}

        if operation == 'payment':
            endpoint = f"v0.1/merchants/{provider.sumup_merchant_code}/readers/{self.reader.terminal_id}/checkout"
            
            if self.sumup_tipping_enabled and self.sumup_tip_rates:
                try:
                    # Convert "10,15,20" -> [0.10, 0.15, 0.20]
                    percentages = [int(rate.strip()) / 100.0 for rate in self.sumup_tip_rates.split(',') if rate.strip()]
                    # Filter valid range 0.01 - 0.99
                    valid_rates = [r for r in percentages if 0.01 <= r <= 0.99]
                    if valid_rates:
                        if 'tip_rates' not in data: 
                             data['tip_rates'] = valid_rates
                except ValueError:
                    _logger.warning("Invalid SumUp tip rates format: %s", self.sumup_tip_rates)

            return provider.sumup_make_request(endpoint, data=data)

        elif operation == 'poll_status':
            client_transaction_id = data.get('client_transaction_id')
            if not client_transaction_id:
                return {'error': {'message': 'Missing client_transaction_id'}}

            endpoint = f"v2.1/merchants/{provider.sumup_merchant_code}/transactions"
            params = {
                'client_transaction_id': client_transaction_id
            }
            response = provider.sumup_make_request(endpoint, method='GET', params=params)

            # The reader checkout is asynchronous and SumUp needs some time to
            # make the transaction queryable through this endpoint. Until then it
            # replies with a 404. That is NOT a failure: the payment may still be
            # in progress or may have just succeeded on the terminal. Report it
            # as a transient PENDING so the front-end keeps polling instead of
            # treating it as an error (which previously caused the endless
            # spinner and, on cashier abort, double charges).
            error = response.get('error') if isinstance(response, dict) else None
            if error and error.get('http_status') == 404:
                return {'status': 'PENDING'}
            # Some SumUp response shapes wrap the result in "items"; normalize to
            # a single transaction object so the front-end can read 'status'.
            if isinstance(response, dict) and response.get('items'):
                return response['items'][0]
            return response

        elif operation == 'cancel':
             endpoint = f"v0.1/merchants/{provider.sumup_merchant_code}/readers/{self.reader.terminal_id}/terminate"
             return provider.sumup_make_request(endpoint, method='POST')
        
        return False

    def sumup_make_refund_request(self, data):
        self.ensure_one()
        if not self.env.user.has_group('point_of_sale.group_pos_user'):
            raise AccessError(_("Only 'group_pos_user' are allowed to send a SumUp refund request"))

        provider = self._get_sumup_payment_provider()
        if not provider:
            return {'error': {'code': 'CONFIG_ERROR', 'message': _("SumUp provider is not configured.")}}

        transaction_id = data.get('transaction_id')
        amount = abs(float(data.get('amount', 0)))

        if not transaction_id:
            return {'error': {'code': 'MISSING_TRANSACTION', 'message': _("Missing original SumUp transaction.")}}
        if amount <= 0:
            return {'error': {'code': 'INVALID_AMOUNT', 'message': _("Refund amount must be greater than zero.")}}

        transaction = self._get_sumup_transaction(provider, transaction_id)

        if not transaction:
            return {'error': {'code': 'TRANSACTION_NOT_FOUND', 'message': _("Original SumUp transaction was not found.")}}

        if transaction.get('status') != 'SUCCESSFUL':
            return {
                'error': {
                    'code': 'INVALID_TRANSACTION_STATUS',
                    'message': _("Only successful SumUp transactions can be refunded."),
                }
            }

        total_amount = transaction.get('amount')
        if total_amount is None:
            return {'error': {'code': 'INVALID_RESPONSE', 'message': _("SumUp did not return the transaction amount.")}}

        refunded_amount = self._get_sumup_refunded_amount(transaction_id)
        remaining_amount = max(total_amount - refunded_amount, 0)

        if amount > remaining_amount:
            return {
                'error': {
                    'code': 'REFUND_LIMIT_EXCEEDED',
                    'message': _(
                        "Refund amount exceeds the remaining refundable SumUp amount."
                    ),
                }
            }

        payload = {}
        if amount < remaining_amount:
            payload['amount'] = amount

        response = provider.sumup_make_request(
            f"v0.1/me/refund/{transaction_id}",
            data=payload,
        )
        if response.get('error'):
            return response

        return {
            'success': True,
            'transaction_id': transaction['id'],
            'amount': amount,
            'remaining_amount': remaining_amount - amount,
        }

    def _get_sumup_transaction(self, provider, transaction_identifier):
        search_params = [
            {'id': transaction_identifier},
            {'transaction_code': transaction_identifier},
        ]
        for params in search_params:
            transaction = provider.sumup_make_request(
                f"v2.1/merchants/{provider.sumup_merchant_code}/transactions",
                method='GET',
                params=params,
            )
            if transaction.get('error'):
                continue
            if transaction.get('items'):
                transaction = transaction['items'][0]
            if transaction.get('id'):
                return transaction
        return False

    def action_update_readers(self):
        ''' Updates the list of readers from SumUp API.
        '''
        self.ensure_one()
        provider = self._get_sumup_payment_provider()
        if not provider:
            raise UserError(_("SumUp provider is not configured or disabled."))

        endpoint = f"v0.1/merchants/{provider.sumup_merchant_code}/readers"
        response = provider.sumup_make_request(endpoint, method='GET')
        
        if 'error' in response:
             raise UserError(_("Failed to update readers: %s") % response['error'].get('message', 'Unknown error'))
        
        items = response.get('items', [])
        affected_readers = self.env['sumup.terminal']
        
        for item in items:
            terminal_id = item.get('id')
            name = item.get('name')
            
            if not terminal_id or not name:
                continue

            existing_reader = self.env['sumup.terminal'].search([('terminal_id', '=', terminal_id)], limit=1)
            if existing_reader:
                existing_reader.write({'name': name})
                affected_readers |= existing_reader
            else:
                new_reader = self.env['sumup.terminal'].create({
                    'name': name,
                    'terminal_id': terminal_id
                })
                affected_readers |= new_reader
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Success"),
                'message': _("%d readers updated.") % len(affected_readers),
                'type': 'success',
                'sticky': False,
            }
        }

    def action_delete_reader(self):
        self.ensure_one()
        if not self.reader:
             return
        
        provider = self._get_sumup_payment_provider()
        reader_to_delete = self.reader
        
        if provider:
             endpoint = f"v0.1/merchants/{provider.sumup_merchant_code}/readers/{reader_to_delete.terminal_id}"
             # We execute the request but ignore failures to ensure Odoo deletion proceeds
             result = provider.sumup_make_request(endpoint, method='DELETE')
             if 'error' in result:
                 _logger.warning("Failed to delete reader from SumUp API: %s", result['error'])

        # Always delete from Odoo
        self.reader = False
        reader_to_delete.unlink()
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Reader Deleted"),
                'message': _("The reader has been removed."),
                'type': 'success',
                'sticky': False,
            }
        }
