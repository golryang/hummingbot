# API Trader

API traders can execute trades via hyperliquid [exchange](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint#place-an-order) endpoints with Based builder code and client id.

Based builder wallet `0x1924b8561eeF20e70Ede628A296175D358BE80e5`
Recommended builder fee: 0.025% (perp), 0.1 (spot)
Client id (cloid): `0xba5ed11067f2cc08ba5ed10000ba5ed1`


Sample perp order
```
{
  "action": {
    "type": "order",
    "orders": [
      {
        "a": 0,
        "b": true,
        "p": "111951",
        "s": "0.0012",
        "r": false,
        "t": {
          "limit": {
            "tif": "Gtc"
          }
        },
        "c": "0xba5ed11067f2cc08ba5ed10000ba5ed1"
      }
    ],
    "grouping": "na",
    "builder": {
      "b": "0x1924b8561eef20e70ede628a296175d358be80e5",
      "f": 25
    }
  },
  "nonce": ..,
  "signature": {
    ...
  }
}
```