from __future__ import annotations


DISCOVERY_QUERY = """
query OrganizationInventory(
  $organization: String!
  $cursor: String
  $pageSize: Int!
) {
  organization(login: $organization) {
    repositories(
      first: $pageSize
      after: $cursor
      orderBy: {field: NAME, direction: ASC}
    ) {
      totalCount
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        id
        nameWithOwner
        url
        visibility
        pushedAt
        isArchived
        isFork
        isTemplate
        diskUsage
        defaultBranchRef {
          name
        }
        languages(first: 100, orderBy: {field: SIZE, direction: DESC}) {
          totalSize
          pageInfo {
            hasNextPage
          }
          edges {
            size
            node {
              name
            }
          }
        }
      }
    }
  }
  rateLimit {
    cost
    remaining
    resetAt
  }
}
"""


PREFLIGHT_QUERY = """
query InventoryPreflight($organization: String!) {
  viewer {
    login
  }
  organization(login: $organization) {
    login
    viewerCanAdminister
    repositories {
      totalCount
    }
  }
  rateLimit {
    cost
    limit
    remaining
    resetAt
  }
}
"""


ROOT_TREE_QUERY = """
query RepositoryRoot(
  $owner: String!
  $name: String!
  $expression: String!
) {
  repository(owner: $owner, name: $name) {
    object(expression: $expression) {
      __typename
      ... on Tree {
        entries {
          name
          type
        }
      }
    }
  }
  rateLimit {
    cost
    remaining
    resetAt
  }
}
"""


ONE_LEVEL_TREE_QUERY = """
query RepositoryRootAndOneLevel(
  $owner: String!
  $name: String!
  $expression: String!
) {
  repository(owner: $owner, name: $name) {
    object(expression: $expression) {
      __typename
      ... on Tree {
        entries {
          name
          type
          object {
            __typename
            ... on Tree {
              entries {
                name
                type
              }
            }
          }
        }
      }
    }
  }
  rateLimit {
    cost
    remaining
    resetAt
  }
}
"""
