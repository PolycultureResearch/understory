# Alpenglow Supply Co.

## What the company does

Alpenglow Supply Co. is a direct-to-consumer outdoor and lifestyle apparel brand. It designs tees, hoodies, jackets, pants, shorts, hats, and socks and sells them through its own web store, in US dollars, to customers mostly in the United States with smaller shares in Canada, the United Kingdom, and Germany. There are no wholesale accounts, no stores, and no subscriptions. Every dollar comes from an order placed on the site.

Traffic arrives through five channels: organic search, paid search, paid social, email, and direct. Paid search and paid social carry ad spend; email and direct are mostly returning customers. Most sessions are on mobile, but desktop converts better. The business is seasonal, with a late-November holiday peak and busier weekends than midweek. A few promo codes run each year (a summer solstice code, Black Friday, a spring sale) and are applied as order-level discounts. Orders over 75 dollars ship free; smaller orders pay a flat fee. History starts in June 2024.

## How the money flows

A session may turn into an order. An order has one or more line items, each with a quantity and a category, and carries a merchandise subtotal, a promo discount if a code was used, and a shipping charge. A shipment leaves about a day later and delivers roughly four days after that. Some orders come back: a return request is filed around nine days after delivery, and the refund is booked on that request date. Cards are charged at order time and a small share of charges fail.

## Metrics in plain words

Gross revenue is the merchandise subtotal of orders placed, before discounts and refunds, dated by order date. Discounts are the promo amounts taken off those orders. Refunds are the value of returns, dated by the day the return was requested. Net revenue is gross revenue minus discounts minus refunds. Shipping charges are not part of any revenue metric.

Payments captured is the value of successful card charges. It includes shipping and is net of discounts, so it will not match gross or net revenue and is not a revenue figure.

Gross margin is line revenue minus unit cost, before discounts, and margin rate is gross margin divided by gross revenue. Both come from line items, which is also where units and category live. Average order value is gross revenue divided by orders.

Conversion rate is session-attributed orders divided by sessions. Only orders that can be tied to a web session count in the numerator, so it is a site conversion rate, not a share of all orders. New customers are counted on the day of their first order. Repeat order share is the share of orders placed by existing customers. Return rate is returned units divided by units sold, each dated by its own day. Average fulfillment days is the mean time from order to delivery, for delivered orders only.

## Gotchas

Refunds lag orders by delivery time plus the request delay, typically two weeks. Net revenue and return rate for the most recent two to three weeks are therefore flattered, and a bad batch shows up in returns weeks after the orders that caused it. Read return rate over a month or more, never as a daily series.

Category exists only on line items and returns, not on orders. Units, gross margin, returned units, and refunds can be sliced by category; gross revenue, discounts, net revenue, and order counts cannot. Reach for margin or units when the question is about a category.

Channel, country, and device on an order describe the session that placed it. The customer table carries the channel and device of a customer's first order, which is a different thing. Marketing spend comes from the ad platforms and covers paid search and paid social only.

## What the data cannot answer

There is no cost data beyond unit cost, so no profit, contribution margin, or return on ad spend after shipping and fulfillment. Promo codes are stamped on orders but are not a dimension, so performance by code is not available. There is no product or SKU dimension, no size or color, and no inventory or stock-out data. Customer lifetime value and cohort retention are not modeled. Why a number moved is not something this data settles on its own.
