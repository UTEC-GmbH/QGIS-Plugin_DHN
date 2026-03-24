"""Module: graph_definitions.py

This module contains dataclasses for graph and network representation.
"""

from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Node:
    """Represents a node in the network graph, defined by its coordinates.

    Attributes:
        x: The x-coordinate of the node.
        y: The y-coordinate of the node.
    """

    x: float
    y: float


@dataclass(frozen=True)
class NetworkEdge:
    """Represents an edge in the network graph.

    Attributes:
        pipe_id: The feature ID of the pipe segment.
        neighbor: The neighboring node connected by this edge.
    """

    pipe_id: int
    neighbor: Node


@dataclass
class NetworkGraph:
    """Holds the graph representation of the pipe network.

    Attributes:
        adjacency: A dictionary mapping nodes to sets of connected network edges.
        degrees: A dictionary mapping nodes to their degree (connection count).
    """

    adjacency: dict[Node, set[NetworkEdge]] = field(default_factory=dict)
    degrees: dict[Node, int] = field(default_factory=dict)


@dataclass
class PipeInfo:
    """Holds information about a pipe segment.

    Attributes:
        dim: The dimension (diameter) of the pipe.
        length: The length of the pipe segment.
    """

    dim: int
    length: float


@dataclass(frozen=True)
class BranchStart:
    """Represents the starting point of a new branch.

    Attributes:
        node: The starting node of the branch.
        first_pipe_id: The ID of the first pipe in the branch, or None if starting
            from a root.
    """

    node: Node
    first_pipe_id: int | None


@dataclass
class MainPipeGraph:
    """Graph representation of the main pipe network.

    Attributes:
        adjacency: A dictionary mapping nodes to lists of connected network edges.
        pipe_info: A dictionary mapping pipe IDs to pipe information.
    """

    adjacency: dict[Node, list[NetworkEdge]]
    pipe_info: dict[int, PipeInfo]


@dataclass
class NetworkOrientation:
    """Orientation of the network relative to a root node.

    Attributes:
        root: The root node used for orientation.
        node_depth: A dictionary mapping nodes to their depth (distance from root).
    """

    root: Node
    node_depth: dict[Node, int]


@dataclass
class TraversalContext:
    """Holds context data for network traversal.

    Attributes:
        adj: The adjacency list of the network.
        pipe_info: Information about the pipes in the network.
        node_depth: Depth of each node relative to the root.
        visited_pipes: A set of pipe IDs that have already been visited.
        branch_queue: A queue of branches waiting to be processed.
    """

    adj: dict[Node, list[NetworkEdge]]
    pipe_info: dict[int, PipeInfo]
    node_depth: dict[Node, int]
    visited_pipes: set[int]
    branch_queue: deque[BranchStart]


@dataclass
class InitializedBranch:
    """Represents a partially traced branch segment.

    Attributes:
        branch_segments: A list of network edges forming the branch.
        current_node: The current node in the traversal.
    """

    branch_segments: list[NetworkEdge]
    current_node: Node
