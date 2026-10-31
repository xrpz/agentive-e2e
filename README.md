# shop

Tiny shop library used to test agentive end to end. Run `uv run pytest`.

## Example

```python
from shop.cart import total

total([10.0, 5.0], discount=0.1)  # 13.5
```
