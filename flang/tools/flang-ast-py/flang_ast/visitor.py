"""Visitor / walker helpers for traversing parse trees."""

from __future__ import annotations

from typing import Callable, Generic, Iterator, TypeVar

from .nodes import Node

T = TypeVar("T")


def walk(node: Node) -> Iterator[Node]:
    """Yield ``node`` and every descendant in pre-order.

    Equivalent to ``node.walk()``; provided as a free function for symmetry
    with ``ast.walk`` from the standard library.
    """
    yield from node.walk()


class NodeVisitor(Generic[T]):
    """Dispatching visitor à la ``ast.NodeVisitor``.

    Subclass and define ``visit_<Kind>`` methods to handle specific node
    kinds.  Methods that exist take precedence over ``generic_visit``.

    ``visit`` returns the result of the chosen handler (defaulting to
    ``None`` from ``generic_visit``).  Use the ``T`` type parameter to
    annotate the return type your visitor produces.

    Example
    -------
    >>> class Counter(NodeVisitor[int]):
    ...     def __init__(self) -> None:
    ...         self.n = 0
    ...     def visit_AssignmentStmt(self, node: Node) -> int:
    ...         self.n += 1
    ...         return self.generic_visit(node)
    ...     def generic_visit(self, node: Node) -> int:
    ...         for child in node.children:
    ...             self.visit(child)
    ...         return self.n
    """

    def visit(self, node: Node) -> T:
        method: Callable[[Node], T] | None = getattr(
            self, f"visit_{node.kind}", None
        )
        if method is not None:
            return method(node)
        return self.generic_visit(node)

    def generic_visit(self, node: Node) -> T:
        """Default handler: recurse into children, return ``None``."""
        for child in node.children:
            self.visit(child)
        # Subclasses with a non-Optional T should override this.
        return None  # type: ignore[return-value]


class NodeTransformer(NodeVisitor[Node]):
    """Visitor that returns a (possibly rebuilt) node.

    ``generic_visit`` recurses into children and substitutes any replacements
    returned by the per-kind methods.  The original tree is not mutated;
    new ``Node`` instances are produced for any path that changed.
    """

    def generic_visit(self, node: Node) -> Node:
        new_children: list[Node] = []
        changed = False
        for child in node.children:
            replacement = self.visit(child)
            if replacement is not child:
                changed = True
            new_children.append(replacement)
        if not changed:
            return node
        return Node(
            kind=node.kind,
            source=node.source,
            fortran=node.fortran,
            label=node.label,
            children=new_children,
        )
