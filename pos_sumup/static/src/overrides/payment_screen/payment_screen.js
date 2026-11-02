/** @odoo-module */

import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

patch(PaymentScreen.prototype, {
    async addNewPaymentLine(paymentMethod) {
        if (paymentMethod.use_payment_terminal === "sumup" && this.isRefundOrder) {
            const refundedOrder = this.currentOrder.lines[0]?.refunded_orderline_id?.order_id;
            if (!refundedOrder) {
                this.notification.add(
                    _t("No refunded order was found for this SumUp refund."),
                    { type: "warning", sticky: false }
                );
                return false;
            }

            const usedTransactionIds = new Set(
                this.currentOrder.payment_ids
                    .map((line) => line.transaction_id)
                    .filter(Boolean)
            );

            const refundedPaymentLine = refundedOrder.payment_ids.find(
                (line) =>
                    line.payment_method_id.use_payment_terminal === "sumup" &&
                    line.transaction_id &&
                    !usedTransactionIds.has(line.transaction_id)
            );

            if (!refundedPaymentLine) {
                this.notification.add(
                    _t("There is no refundable SumUp transaction available on the original order."),
                    { type: "warning", sticky: false }
                );
                return false;
            }

            const result = await super.addNewPaymentLine(paymentMethod);
            if (!result) {
                return result;
            }

            const newPaymentLine = this.paymentLines.at(-1);
            newPaymentLine.transaction_id = refundedPaymentLine.transaction_id;
            return result;
        }

        return await super.addNewPaymentLine(paymentMethod);
    },

    async deletePaymentLine(uuid) {
        const line = this.paymentLines.find((l) => l.uuid === uuid);

        // Before cancelling/removing a SumUp payment that is still in progress,
        // make sure it did not already succeed on the terminal. If it did, book
        // it and KEEP the line (the core would otherwise remove it
        // unconditionally), preventing the double charge. Only then fall back to
        // the normal cancel (which terminates the reader and removes the line).
        if (
            line &&
            line.payment_method_id?.use_payment_terminal === "sumup" &&
            line.transaction_id &&
            ["waiting", "waitingCard", "timeout"].includes(line.getPaymentStatus())
        ) {
            const terminal = line.payment_method_id.payment_terminal;
            const booked = await terminal.bookIfAlreadySuccessful(line);
            if (booked) {
                this.numberBuffer.reset();
                // Finish the order automatically if it is now fully paid, so the
                // cashier is not left on a "stuck in payment" screen.
                const order = this.currentOrder;
                if (
                    order.isPaid() &&
                    this.pos.config.auto_validate_terminal_payment &&
                    !order.isRefundInProcess()
                ) {
                    this.validateOrder(false);
                }
                return;
            }
        }

        return super.deletePaymentLine(uuid);
    },
});
