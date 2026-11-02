## Explainer

**What changes:** adds a public `validate_new_password()` that rejects new passwords under 8 characters, but nothing in `src/` calls it yet, so no password is checked today.

- New: `validate_new_password` raises `ValueError` below `MIN_PASSWORD_LENGTH = 8` (`src/shop/auth/__init__.py:4`, `src/shop/auth/__init__.py:12-18`).
- Unchanged: `check_password` (`src/shop/auth/__init__.py:7-9`), so existing short passwords still log in (`tests/test_auth.py#test_existing_short_password_still_checks`).
- You decide: is 8 the right minimum, and should this PR also call the check where a password is set (no such code exists in `src/` today)?
- Tier 2: `src/shop/auth/__init__.py` matches the trust boundary `**/auth/**`.

### Before

```mermaid
flowchart TD
  accTitle: Before - shop.auth on main
  accDescr: App code calls check_password at login and gets True or False. shop.auth has no rule for new passwords.
  caller(["App code using shop.auth"])
  cmp["Compare passwords<br/>check_password()"]
  match(["True or False"])
  caller -->|login| cmp
  cmp --> match
```

### After

```mermaid
flowchart TD
  accTitle: After - shop.auth with this PR
  accDescr: Login is unchanged. A new function validate_new_password rejects passwords shorter than 8 characters with ValueError; nothing in src calls it yet.
  caller(["App code using shop.auth"])
  cmp["Compare passwords<br/>check_password()"]
  match(["True or False"])
  val["NEW<br/>Check length is 8 or more<br/>validate_new_password()"]:::added
  reject(["NEW<br/>ValueError"]):::added
  accept(["NEW<br/>Returns, nothing saved"]):::added
  caller -->|login| cmp
  cmp --> match
  caller ==>|"set password<br/>(no caller in src/ yet)"| val
  val ==>|"under 8 chars"| reject
  val ==>|"8 chars or more"| accept
  classDef added stroke:#0969da,stroke-width:3px
  classDef changed stroke:#0969da,stroke-width:3px
  classDef removed stroke-width:2px,stroke-dasharray:6 4
```

How to read: NEW / CHANGED / REMOVED name the change · thick blue border = new or changed step · dashed border = removed step · thick arrow = new call · dotted arrow = removed call · plain = unchanged
