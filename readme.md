# EC2 Image Builder Proof of Concept


In December 2019 AWS released EC2 Image Builder, a rather interesting service for generating AMIs:

It is a service that makes it easier and faster to build and maintain secure images. Image Builder simplifies the creation, patching, testing, distribution, and sharing of Linux or Windows Server images.

Unfortunately the Console for EC2 Image Builder has quite a few limitations which don't reflect the strengths of the service.

This framework was created as a proof of concept to show how to create the Image Pipeline and all of its dependencies and is for testing purposes (i.e not production ready). 

The Pipeline definition is done via a yaml file which is consumed by the script, presently it will generate a new version of any versionable resources and then recreate the pipeline in order to deploy that new version. A flag is available to recreate all non-versionable resource.

Two pipeline configurations are provided:

* Hardened AMI following requirements of CIS Amazon Linux 2 Benchmark version 1.0.0
* Cloud Custodian AMI

No tests are done to see if the latest version is the same as that defined.

### Requirements

* python 3.6+
* boto3
* aws-auth-helper
* pyyaml

### Setup

Install the python dependencies with `pip install -r requirements.txt`

### Usage

The following arguments are available (in addition to those provided by the aws-auth-helper library):

```
  --pipeline-def PIPELINE_DEF
                        File containing the pipeline definition, referencing
                        components and such
  --component-bucket COMPONENT_BUCKET
                        S3 Bucket to temporarily store component definition in
                        (optional). Use if boto tells you the component has
                        too many characters
  --start-pipeline      Start Pipeline after creation
  --update              Recreate non-versioned resources instead of reusing
                        them
  --debug               Increase output verbosity
```


Execute the tool by providing it with the location of the pipeline definition, in this case `custodian.yaml`. Execute the script like so:

`python build.py --region eu-west-1 --pipeline-def custodian.yaml `

