"""Messages introduced by the shipping-followup feature."""

MESSAGES = {
    "zh": {
        "unknown_customer": "客户身份无效。",
        "not_found": "未找到该工单或订单。",
        "not_shipped": "只能为已发货且尚未送达的订单创建物流跟进工单。",
        "event_conflict": "该事件标识已经用于其他事件内容。",
        "execution_failed": "物流查询未完成，请使用相同事件标识重试。",
        "goal": "持续跟进订单物流，直到订单送达或客户撤销。",
        "delayed": "订单 {order_id} 的物流出现延迟，系统将继续跟进。",
        "in_transit": "订单 {order_id} 正在运输中，系统将继续跟进。",
        "delivered": "订单 {order_id} 已送达，本次物流跟进已完成。",
        "demo_done": "演示完成：工单在重启后继续处理，重复事件未重复通知，送达后工单已完成。",
    },
    "en": {
        "unknown_customer": "Unknown customer.",
        "not_found": "Ticket or order not found.",
        "not_shipped": "Shipping followups require a shipped order that has not been delivered.",
        "event_conflict": "This event ID has already been used with a different payload.",
        "execution_failed": "Tracking could not be verified. Retry with the same event ID.",
        "goal": "Follow this shipment until delivery or customer cancellation.",
        "delayed": "Shipment for order {order_id} is delayed. Followup remains active.",
        "in_transit": "Order {order_id} is in transit. Followup remains active.",
        "delivered": "Order {order_id} has been delivered. Shipping followup is complete.",
        "demo_done": "Demo complete: restart recovery, duplicate suppression and delivery completion passed.",
    },
}


def translate(key: str, locale: str = "zh", **values: str) -> str:
    return MESSAGES[locale][key].format(**values)
