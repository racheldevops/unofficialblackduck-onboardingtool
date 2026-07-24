# Unofficial BlackDuck GitHub Organization Onboarding tool

THIS PROJECT IS IN ACTIVE DEV AND IS UNFINISHED IN ITS CURRENT STATE

There was an enterprise business need to onboard Blackduck SCA scanning across multiple organisations
that each contained multiple thousands of repositories, with certain repo's & language types being excluded
due to another scanning solution being used for them. The official BlackDuck App does not provide enough 
granularity at scale to inventory existing real-estate and target repositories in a controlled rollout. 

This solution solves that use-case. 
Github_Inventory uses GraphQL to categorise repositories for targetting and Github_Onboard deploys specific
rulesets and yaml for bespoke, immutable and centrally controlled scanning across enterprise real-estates.


# License
This is not an officially supported pathway, and is at used at own risk
