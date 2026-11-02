/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { PaymentInterface } from "@point_of_sale/app/utils/payment/payment_interface";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

export class PaymentSumup extends PaymentInterface {
    setup() {
        super.setup(...arguments);
    }

    get fastPayments() {
        return true;
    }

    sendPaymentRequest(uuid) {
        super.sendPaymentRequest(uuid);
        return this._process_sumup_request(uuid);
    }

    sendPaymentCancel(order, uuid) {
        super.sendPaymentCancel(order, uuid);
        // The "did the payment already succeed?" final check happens in the
        // PaymentScreen.deletePaymentLine override (that is where we can decide
        // whether the line is kept). By the time we get here the decision to
        // terminate has been made, so just terminate the reader checkout.
        return this._call_sumup({}, "cancel");
    }

    /**
     * Called from the PaymentScreen before a SumUp line is cancelled/removed.
     * If the payment already succeeded on the terminal, book it (so the cashier
     * is not tricked into a second, cash payment -> the double-charge bug) and
     * return true so the screen keeps the line instead of terminating/removing.
     */
    async bookIfAlreadySuccessful(line) {
        const client_transaction_id = line && line.transaction_id;
        if (!client_transaction_id) {
            return false;
        }
        const status = await this._call_sumup(
            { client_transaction_id: client_transaction_id },
            "poll_status"
        ).catch(() => null);

        if (status && !status.error && status.status === "SUCCESSFUL") {
            await this._finalize_successful_payment(line, status);
            this._show_error(
                _t(
                    "This SumUp payment was already completed on the terminal and has been booked. It was NOT cancelled."
                ),
                _t("Payment already completed")
            );
            return true;
        }
        return false;
    }

    pending_sumup_line() {
        return this.pos.getPendingPaymentLine("sumup");
    }

    // Returns the payment line for `uuid` only if it is still part of the
    // current order (i.e. not removed by a concurrent cancel). Used by the poll
    // loop so it never resolves a result onto a removed line, which would crash
    // the core's handlePaymentResponse (reading payment_method_id of undefined).
    _live_line(uuid) {
        const order = this.pos.getOrder();
        return order && order.payment_ids.find((paymentLine) => paymentLine.uuid === uuid);
    }

    _handle_odoo_connection_failure(data = {}) {
        var line = this.pending_sumup_line();
        if (line) {
            line.setPaymentStatus("retry");
        }
        this._show_error(
            _t(
                "Could not connect to the Odoo server, please check your internet connection and try again."
            )
        );
        return Promise.reject(data);
    }

    _call_sumup(data, operation = false) {
        return this.env.services.orm.silent
            .call("pos.payment.method", "proxy_sumup_request", [
                [this.payment_method_id.id],
                data,
                operation,
            ])
            .catch(this._handle_odoo_connection_failure.bind(this));
    }

    _sumup_pay_data(line) {
        var currency = this.pos.currency;
        var amount = line.amount;
        var minor_unit = currency.decimal_places;
        var value = Math.round(amount * Math.pow(10, minor_unit));

        return {
            "total_amount": {
                "value": value,
                "currency": currency.name,
                "minor_unit": minor_unit
            }
        };
    }

    _sumup_refund_data(line) {
        return {
            amount: Math.abs(line.amount),
            transaction_id: line.transaction_id,
        };
    }

    _handle_sumup_error(error) {
        let msg = error.message;
        if (error.code === 'TIMEOUT') {
            msg = _t("The reader did not respond in time. Please try again.");
        } else if (error.code === 'NETWORK_ERROR') {
            msg = _t("Could not connect to SumUp. Check internet.");
        } else if (error.code === 'AUTH_ERROR') {
            msg = _t("Authentication failed. Check SumUp configuration.");
        }
        this._show_error(_t("SumUp Error: %s", msg));
    }

    _process_sumup_request(uuid) {
        var order = this.pos.getOrder();
        var line = order.payment_ids.find((paymentLine) => paymentLine.uuid === uuid);

        if (order.isRefund) {
            return this._sumup_refund(line);
        }

        if (line.amount < 0) {
            this._show_error(_t("Cannot process transactions with negative amount."));
            return Promise.resolve();
        }

        var data = this._sumup_pay_data(line);

        return this._call_sumup(data, 'payment').then((response) => {
            if (response.error) {
                this._handle_sumup_error(response.error);
                line.setPaymentStatus("force_done");
                return false;
            }

            // Checkout created, start polling
            // The ID is in response.data.client_transaction_id
            var client_transaction_id = response.data && response.data.client_transaction_id;

            if (!client_transaction_id) {
                this._show_error(_t("SumUp Error: No client_transaction_id in response"));
                line.setPaymentStatus("retry");
                return false;
            }

            line.transaction_id = client_transaction_id; // Store for polling
            return this._poll_for_status(uuid, client_transaction_id);
        });
    }

    _sumup_refund(line) {
        const transaction_id = line.transaction_id;
        if (!transaction_id) {
            this._show_error(_t("No original SumUp transaction is linked to this refund."));
            line.setPaymentStatus("retry");
            return Promise.resolve(false);
        }

        line.setPaymentStatus("waitingCard");
        const data = this._sumup_refund_data(line);

        return this.env.services.orm.silent
            .call("pos.payment.method", "sumup_make_refund_request", [[this.payment_method_id.id], data])
            .then((response) => {
                if (response.error) {
                    this._handle_sumup_error(response.error);
                    line.setPaymentStatus("retry");
                    return false;
                }

                line.transaction_id = response.transaction_id;
                line.setPaymentStatus("done");
                return true;
            })
            .catch(this._handle_odoo_connection_failure.bind(this));
    }

    async _poll_for_status(uuid, client_transaction_id) {
        // Stop conditions that must be checked at every step, because the cashier
        // can cancel (remove the line) or the cancel path can book it (set it
        // "done") while we are mid-poll:
        //  - line removed   -> return false (core sets the orphan to retry, no crash)
        //  - line already done -> return true (keep it done, don't downgrade)
        let line = this._live_line(uuid);
        if (!line) {
            return false;
        }
        if (line.payment_status === "done") {
            return true;
        }

        // Poll every 3 seconds
        await new Promise(resolve => setTimeout(resolve, 3000));

        line = this._live_line(uuid);
        if (!line) {
            return false;
        }
        if (line.payment_status === "done") {
            return true;
        }

        var data = { 'client_transaction_id': client_transaction_id };
        const response = await this._call_sumup(data, 'poll_status');

        // Re-check liveness after the network round-trip.
        line = this._live_line(uuid);
        if (!line) {
            return false;
        }
        if (line.payment_status === "done") {
            return true;
        }

        // A genuine transport error (Odoo unreachable) is already handled by
        // _call_sumup -> _handle_odoo_connection_failure (it sets the line to
        // "retry" and rejects), so we never reach this point in that case.
        if (response && response.error) {
            // SumUp-side error while the payment may still be live on the
            // terminal. We must NOT abandon the line here: doing so is exactly
            // what produced the double charges. Keep polling; the only
            // deliberate exit is a definitive status or the cashier pressing X
            // (which runs a final status check before terminating).
            console.warn("SumUp poll: transient error, keep polling", response.error);
            return this._poll_for_status(uuid, client_transaction_id);
        }

        const status = response && response.status;

        if (status === 'SUCCESSFUL') {
            return this._finalize_successful_payment(line, response);
        } else if (status === 'FAILED' || status === 'CANCELLED') {
            this._show_error(_t("Payment failed."));
            line.setPaymentStatus("retry");
            return false;
        } else if (status === 'EXPIRED') {
            this._show_error(_t("Payment expired."));
            line.setPaymentStatus("retry");
            return false;
        }

        // PENDING, our 404 -> PENDING mapping, or any other intermediate /
        // unknown status: keep polling until SumUp returns a definitive result.
        // Never give up on a transaction that might still succeed.
        return this._poll_for_status(uuid, client_transaction_id);
    }

    async _finalize_successful_payment(line, response) {
        // The poll loop and the cancel path can both observe SUCCESSFUL for the
        // same transaction. Guard against booking it twice (which could e.g.
        // apply the tip twice). setPaymentStatus("done") below runs before any
        // await, so a second concurrent call sees "done" and bails out here.
        if (line.payment_status === "done") {
            return true;
        }
        line.setPaymentStatus("done");
        line.transaction_id = response.id || response.transaction_code || line.transaction_id;

        // Card details for receipt
        if (response.card) {
            line.card_type = response.card.type;
            line.cardholder_name = response.card.last_4_digits;
        }

        // Handle Tipping: if authorized amount > requested amount
        if (response.amount && response.amount > line.amount) {
            const tip_amount = response.amount - line.amount;
            if (this.pos.config.tip_product_id) {
                await this.pos.setTip(tip_amount);
            }
            line.setAmount(response.amount);
        }

        return true;
    }

    _show_error(msg, title) {
        if (!title) {
            title = _t("SumUp Error");
        }
        this.env.services.dialog.add(AlertDialog, {
            title: title,
            body: msg,
        });
    }
}
