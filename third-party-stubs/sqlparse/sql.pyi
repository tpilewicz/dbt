from typing import Tuple, Iterable


class Token:
    def __init__(self, ttype, value):
        ...

    is_keyword: bool
    normalized: str


class TokenList(Token):
    tokens: Tuple[Token]

    def __getitem__(self, key) -> Token:
        ...

    def __iter__(self) -> Iterable[Token]:
        ...

    def insert_before(self, where, token, skip_ws=True):
        ...

    def insert_after(self, where, token, skip_ws=True):
        ...

    def token_first(self) -> Token:
        ...


class Statement(TokenList):
    ...
