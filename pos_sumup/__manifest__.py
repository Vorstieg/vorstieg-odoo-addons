# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
{
    'name': 'POS Sumup',
    'version': '1.1',
    'category': 'Sales/Point of Sale',
    'sequence': 6,
    'summary': 'Integrate your POS with a SumUp payment terminal',
    'data': [
        'security/ir.model.access.csv',
        'views/pos_payment_pairing_wizard.xml',
        'views/pos_payment_method_views.xml',

        'data/payment_provider_data.xml',
    ],
    'depends': ['point_of_sale', 'payment'],
    'installable': True,
    'author': 'Vorstieg Software FlexCo',
    'website': 'https://www.vorstieg.eu',
    'assets': {
        'point_of_sale._assets_pos': [
            'pos_sumup/static/**/*',
        ],
    },
    'license': 'LGPL-3',
    'images': ['images/thumbnail.png']
}
