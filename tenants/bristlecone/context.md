# Bristlecone Botanicals

## What the company does

Bristlecone Botanicals is a herbal tea and wellness brand. It makes three product categories, teas, tinctures, and capsules, and sells them wholesale by the case. Its customers are retail chains and distributors, not consumers. Retail partners include a natural-foods chain, a grocery chain, a mass merchant, an online retailer, and a set of independents; two distributors cover the west and the east. Demand peaks in deep winter with cold and flu season and is flat across the days of the week. History starts in July 2024, in US dollars.

## How the money flows

Bristlecone's revenue is what it ships. A shipment line is a number of cases of one product to one account at a list case price, less any trade discount. Retailers and distributors reorder as consumers buy the product off the shelf, so wholesale shipments follow consumer sell-through with a lag of about one to two weeks.

Consumer sell-through arrives separately as weekly point-of-sale scan data from the retailers: units and dollars scanned per product per banner per week. Trade promotions are off-invoice discounts offered to a retailer for a category over a two-week window; they lift scans at that banner and, after the replenishment lag, shipments. Brand marketing runs on paid social, paid search, and podcasts and builds awareness; it does not move sales in any measurable way in this data.

## Metrics in plain words

Shipped cases is the number of wholesale cases shipped, by ship date. Wholesale gross revenue is shipment value at list case prices. Trade spend is the value of trade discounts granted. Wholesale net revenue is gross revenue less trade spend and is the company's topline. Average case price is net revenue divided by cases shipped, the realized price per case.

POS units and POS dollars are consumer units and retail dollars scanned at retail. They describe what shoppers bought at the retailer's price, not what Bristlecone earned. Marketing spend is brand advertising spend by channel.

## Gotchas

Scan data is weekly and each week is keyed to its start day. Daily series for POS metrics carry a value on the week start day and nothing on the other six, so read them at week grain or coarser, and never compare a POS day to a shipment day.

Shipments and scans are on different clocks. A promotion shows in POS first and in shipments one to two weeks later; a demand drop shows in the same order. A week where net revenue and POS disagree is usually the lag, not a data problem.

Retailer banner exists only on scan data. Shipments carry the channel type of the account (natural, grocery, mass, online, independents, or distributor) but not the banner, and distributor shipments cannot be traced to the retailer that eventually sold the product.

Do not attribute shipments or scans to marketing spend. Brand marketing is decoupled from sales here by design, and a spend cut or increase moves marketing spend only.

Trade spend is concentrated in promotion windows, so average case price dips in those weeks and recovers after; a falling case price in a promotion week is the discount, not a pricing problem. Distributors buy on behalf of many small accounts, so a single distributor order can be large and lumpy compared with direct retail shipments, and week grain smooths this better than day grain.

## What the data cannot answer

There is no cost of goods, so no margin or profit. There is no inventory, either at Bristlecone or on retailer shelves, so out-of-stocks and days of supply are unknown. Products are not a dimension, so questions are answered at category level, not per product. Promotion return on investment, retailer margin, consumer price, and cash collections are not available. Why a number moved is not something this data settles on its own.
