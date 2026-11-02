import json

import requests
from odoo import fields, models, _
from odoo.exceptions import UserError

from ..const import SUMUP_BASE_URL

base_url = "https://api.sumup.com/v0.1"


class SumUpReaderPairingWizard(models.TransientModel):
    _name = 'sumup.reader.pairing.wizard'
    _description = 'SumUp Reader Pairing'

    pairing_code = fields.Char(
        string='Pairing Code',
        required=True,
        help='The pairing code displayed on the SumUp terminal.'
    )
    reader_name = fields.Char(
        string='Reader Name',
        required=True,
        default=lambda self: _('New SumUp Terminal'),
        help='A friendly name to identify the terminal in Odoo.'
    )

    def action_pair_reader(self):
        sumup_provider = self._get_sumup_payment_provider()

        endpoint = f"v0.1/merchants/{sumup_provider.sumup_merchant_code}/readers"
        url = SUMUP_BASE_URL + endpoint

        headers = {
            'Authorization': f'Bearer {sumup_provider.sumup_api_key}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

        payload = {
            "pairing_code": self.pairing_code,
            "name": self.reader_name,
            "meta": {}
        }
        try:
            response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
        except requests.exceptions.RequestException as e:
            raise UserError(_("Could not reach SumUp: %s", e))

        if response.status_code != 201:
            # Surface the real SumUp error instead of crashing on a missing
            # 'message' key (the error body shape varies: message / error_message
            # / error_description / error, and is sometimes not JSON at all).
            try:
                body = response.json()
            except ValueError:
                body = {}
            msg = (
                body.get('message')
                or body.get('error_message')
                or body.get('error_description')
                or body.get('error')
                or (response.text or '').strip()
                or _("Unknown error")
            )
            raise UserError(_(
                "SumUp reader pairing failed (HTTP %(code)s): %(msg)s\n\n"
                "Check that the Merchant Code and API Key on the SumUp provider are "
                "correct and enabled, and that the pairing code is still valid "
                "(they expire quickly — generate a fresh one if needed).",
                code=response.status_code, msg=msg,
            ))

        data = response.json()
        self.env['sumup.terminal'].create({
            'name': data['name'],
            'terminal_id': data['id'],
        })

        return {'type': 'ir.actions.act_window_close'}

    def _get_sumup_payment_provider(self):
        sumup_payment_provider = self.env['payment.provider'].search([
            ('code', '=', 'sumup'),
            ('company_id', '=', self.env.company.id)
        ], limit=1)

        if not sumup_payment_provider:
            raise UserError(_("Sumup payment provider for company %s is missing", self.env.company.name))

        return sumup_payment_provider
