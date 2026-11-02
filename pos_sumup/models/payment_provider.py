# coding: utf-8
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
import requests
from odoo import fields, models
from ..const import SUMUP_BASE_URL

_logger = logging.getLogger(__name__)

class PaymentProvider(models.Model):
    _inherit = 'payment.provider'
    
    code = fields.Selection(selection_add=[('sumup', 'Sumup')], ondelete={'sumup': 'set default'})
    sumup_merchant_code = fields.Char(string='Merchant Code', required_if_provider='sumup')
    sumup_api_key = fields.Char(string='API Key', required_if_provider='sumup')

    def sumup_make_request(self, endpoint, data=None, method='POST', params=None):
        self.ensure_one()
        url = SUMUP_BASE_URL + endpoint
        
        headers = {
            'Authorization': f'Bearer {self.sumup_api_key}',
            'Content-Type': 'application/json',
        }

        try:
            if method == 'POST':
                response = requests.post(url, headers=headers, json=data, timeout=60)
            elif method == 'DELETE':
                response = requests.delete(url, headers=headers, json=data, timeout=60)
            else:
                response = requests.get(url, headers=headers, params=params, timeout=10)
            
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        except requests.exceptions.Timeout:
            _logger.error("SumUp API Timeout")
            return {'error': {'code': 'TIMEOUT', 'message': 'Connection timed out.'}}
        except requests.exceptions.ConnectionError:
            _logger.error("SumUp Connection Error")
            return {'error': {'code': 'NETWORK_ERROR', 'message': 'Could not connect to SumUp.'}}
        except requests.exceptions.HTTPError as e:
            status_code = response.status_code
            # A 404 is expected while polling for a transaction that SumUp has
            # not indexed yet (the reader checkout is asynchronous). Do not spam
            # the log with errors for it; callers decide how to treat it via the
            # 'http_status' key below.
            body_text = (response.text or '')[:1000]
            if status_code == 404:
                _logger.info("SumUp API 404 (resource not found / not yet available): %s", url)
            else:
                _logger.error("SumUp API HTTP Error %s for %s | body=%s", status_code, url, body_text)

            if status_code in (401, 403):
                 return {'error': {'code': 'AUTH_ERROR', 'message': 'Authentication failed. Check API Key.', 'http_status': status_code}}

            # Surface the real SumUp validation detail (the body shape varies:
            # dict with message/error_message/detail, or a list of field errors).
            error_code = 'API_ERROR'
            error_msg = None
            try:
                error_data = response.json()
            except ValueError:
                error_data = None
            if isinstance(error_data, dict):
                error_code = error_data.get('error_code', error_code)
                error_msg = (error_data.get('message') or error_data.get('error_message')
                             or error_data.get('error_description') or error_data.get('detail'))
                if not error_msg and error_data.get('errors'):
                    error_msg = str(error_data['errors'])
            elif isinstance(error_data, list) and error_data:
                parts = []
                for item in error_data:
                    if isinstance(item, dict):
                        parts.append('%s: %s' % (
                            item.get('param', '?'),
                            item.get('message') or item.get('error_message') or item,
                        ))
                    else:
                        parts.append(str(item))
                error_msg = '; '.join(parts)
            if not error_msg:
                error_msg = body_text or str(e)

            return {'error': {'code': error_code, 'message': f"SumUp: {error_msg}", 'http_status': status_code}}
        except requests.exceptions.RequestException as e:
            _logger.error("SumUp API Error: %s", e)
            return {'error': {'code': 'UNKNOWN_ERROR', 'message': str(e)}}
