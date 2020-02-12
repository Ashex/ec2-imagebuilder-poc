"""
author: Ahmed Osman <aosman@tibco.com>
version: 1.0.1
"""
import boto3
import awsauthhelper
import argparse
import logging
import sys
import yaml
import json
from io import StringIO
from datetime import datetime



def parseargs():
    argparser = argparse.ArgumentParser(
        description='Create EC2 Image Builder pipeline and assets'
    )
    argparser.add_argument(
        '--pipeline-def',
        help='File containing the pipeline definition, referencing components and such',
        default=None,
        required=True,
        dest='pipeline_def'
    )
    argparser.add_argument(
        '--component-bucket',
        help='S3 Bucket to temporarily store component definition in (optional).'
             ' Use if boto tells you the component has too many characters',
        default=None,
        required=False,
        dest='component_bucket'
    )
    argparser.add_argument(
        '--start-pipeline',
        help='Start Pipeline after creation',
        default=False,
        action='store_true',
        dest='start_pipeline'
    )
    argparser.add_argument(
        '--update',
        help='Recreate non-versioned resources instead of reusing them',
        default=False,
        action='store_true'
    )
    argparser.add_argument(
        '--debug',
        help='Increase output verbosity',
        default=False,
        action='store_true'
    )
    argoptions = awsauthhelper.AWSArgumentParser(
        role_session_name='ec2_image_builder',
        region='us-east-1',
        parents=[argparser]
    )

    credentials = awsauthhelper.Credentials(**vars(argoptions.parse_args()))
    command = argoptions.parse_args(namespace=CreateImagePipeline(
        credentials=credentials,
        session=credentials.create_session()
    ))

    credentials.use_as_global()

    return command


"""
Load Pipeline definition and perform the following:
1. Create all components
    a. If component arn is specified, verify it exists with get_component
    b. return list of dictionary [{ 'component': 'arn'}]
2. Create Image Recipe
    - Return arn
3. Create Infrastructure Configuration
    - Return arn
4. Create Distribution Configuration
    - Return arn
5. Create Pipeline
    - Return arn
6. Optional: start Pipeline
"""


class CreateImagePipeline(argparse.Namespace):
    def __init__(self, **kwargs):
        self.session = None
        self.pipeline_name = None
        self.pipeline_def = None
        self.component_bucket = None
        self.start_pipeline = False
        self.update = False
        self.debug = False
        self.logger = None
        super(CreateImagePipeline, self).__init__(**kwargs)

    def setup_logging(self):
        self.logger = logging.getLogger('CreateImagePipeline')
        formatter = logging.Formatter('[%(asctime)s %(levelname)s] %(message)s')
        # Prevent default handler from being used
        self.logger.propagate = False
        console_handler = logging.StreamHandler(stream=sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(logging.DEBUG)
        self.logger.addHandler(console_handler)
        if self.debug:
            self.logger.setLevel(logging.DEBUG)
        else:
            self.logger.setLevel(logging.INFO)

    @staticmethod
    def get_session_details():
        sts = boto3.client('sts')
        iam = boto3.client('iam')
        session_details = {
            'caller_id': sts.get_caller_identity(),
            # Return empty string if no alias is associated with the account
            'account_alias': iam.list_account_aliases().get('AccountAliases', '')
        }

        return session_details

    def run(self):
        self.setup_logging()
        self.logger.info('Obtaining Session details')
        session_details = self.get_session_details()
        self.logger.debug(session_details)
        self.logger.info('\nAccount: {account_id}\n'
                         'Alias: {alias}\n'
                         'User: {user}\n'.format(
                            account_id=session_details['caller_id']['Account'],
                            alias=session_details['account_alias'],
                            user=session_details['caller_id']['Arn'].split('/')[1]
                            )
                         )
        with open(self.pipeline_def, 'r') as f:
            pipeline_dict = yaml.safe_load(f)
        if self.update:
            self.logger.info('Complete update requested! Deleting non-versioned pipeline resources')
            self.delete_pipeline_resources(pipeline_dict)

        self.logger.info('Creating Components')
        component_list = self.create_components(pipeline_dict['platform'], pipeline_dict['components'])
        self.logger.info('Creating Image Recipe')
        recipe_arn = self.create_image_recipe(component_list, pipeline_dict['image-recipe'])
        if pipeline_dict['instance-profile'].get('file') is not None:
            self.logger.info('Creating Instance Profile')
            instance_profile_name = self.create_instance_profile(pipeline_dict['instance-profile'], session_details['caller_id']['Account'])
        else:
            instance_profile_name = pipeline_dict['instance-profile'].get('name')
        self.logger.info('Creating Infrastructure Configuration')
        infrastructure_arn = self.create_infrastructure_config(instance_profile_name,
                                                               pipeline_dict['infrastructure-configuration'])
        self.logger.info('Creating Distribution Configuration')
        distribution_arn = self.create_distribution_configuration(pipeline_dict['distribution-configuration'])
        self.logger.info('Creating Pipeline')
        pipeline_arn = self.create_image_pipeline(pipeline_dict['pipeline-name'], recipe_arn, infrastructure_arn,
                                                  distribution_arn)
        if self.start_pipeline:
            self.logger.info('Starting Pipeline')
            image_arn = self.start_image_pipeline(pipeline_arn)
            self.logger.info(f'Creating image {image_arn}')

    def create_instance_profile(self, profile_dict, account_id):
        resource_name = 'imagebuilder-{}'.format(profile_dict['name'])
        iam = boto3.client('iam')

        # Syntax validation done via json.load
        with open(profile_dict['file'], 'r') as f:
            policy_dict = json.load(f)

        if len(list(filter(lambda d: 'ec2-image-builder-default' in d['InstanceProfileName'],
                           iam.list_instance_profiles(PathPrefix='/imagebuilder/')['InstanceProfiles']))) > 0:
            if self.update:
                self.logger.warning(f'Instance Profile {resource_name} exists! Updating associated iam role policy')
                try:
                    iam.create_policy_version(
                        PolicyArn=f'arn:aws:iam::{account_id}:policy/imagebuilder/{resource_name}',
                        PolicyDocument=json.dumps(policy_dict),
                        SetAsDefault=True
                    )
                    # Drop the older policy version
                except iam.exceptions.LimitExceededException:
                    self.logger.debug('5 policy version limit encountered, dropping oldest version')
                    response = iam.list_policy_versions(
                        PolicyArn=f'arn:aws:iam::{account_id}:policy/imagebuilder/{resource_name}'
                    )
                    iam.delete_policy_version(
                        PolicyArn=f'arn:aws:iam::{account_id}:policy/imagebuilder/{resource_name}',
                        VersionId=response['Versions'][-1]['VersionId']
                    )
                    iam.create_policy_version(
                        PolicyArn=f'arn:aws:iam::{account_id}:policy/imagebuilder/{resource_name}',
                        PolicyDocument=json.dumps(policy_dict),
                        SetAsDefault=True
                    )

                return resource_name
            else:
                self.logger.warning(f'Instance Profile {resource_name} exists! Skipping creation')
                return resource_name
        iam.create_instance_profile(InstanceProfileName=resource_name,
                                    Path='/imagebuilder/')
        iam.create_role(
            Path='/imagebuilder/',
            RoleName=resource_name,
            AssumeRolePolicyDocument=json.dumps({
                'Version': '2012-10-17',
                'Statement': [
                    {
                        'Effect': 'Allow',
                        'Principal': {
                            'Service': 'ec2.amazonaws.com'
                        },
                        'Action': 'sts:AssumeRole'
                    }
                ]
            }),
            Description='Role for Image Builder',

        )

        response = iam.create_policy(
            PolicyName=resource_name,
            Path='/imagebuilder/',
            PolicyDocument=json.dumps(policy_dict)
        )
        policy_arn = response['Policy']['Arn']

        iam.attach_role_policy(
            RoleName=resource_name,
            PolicyArn=policy_arn
        )

        iam.add_role_to_instance_profile(
            InstanceProfileName=resource_name,
            RoleName=resource_name
        )

        self.logger.debug(resource_name)

        return resource_name

    def create_components(self, platform, component_def):
        imagebuilder = boto3.client('imagebuilder')
        component_list = []
        for component in component_def:
            component_name = ''
            for key in component:
                component_name = key
            if component[component_name].get('arn') is not None:
                # Verify that the Component provided exists to prevent downstream issues
                try:
                    imagebuilder.get_component(componentBuildVersionArn=component[component_name]['arn'])
                    component_list.append({'componentArn': component[component_name]['arn']})
                except imagebuilder.exceptions.ResourceNotFoundException:
                    self.logger.error(f'The specified arn for {component_name} is invalid!')
                # There's a weird API bug where arns need /1 appended
                # https://github.com/boto/boto3/issues/2224
                except imagebuilder.exceptions.InvalidParameterValueException:
                    imagebuilder.get_component(componentBuildVersionArn=component[component_name]['arn'] + '/1')
                    component_list.append({'componentArn': component[component_name]['arn'] + '/1'})
            else:
                # If the component exists, Create a new revision by incrementing the version by 0.0.1
                response = imagebuilder.list_components(owner='Self',
                                                        filters=[{'name': 'name',
                                                                  'values': [component_name]
                                                                  }]
                                                        )
                minor = 1 + len(response['componentVersionList'])
                version = f'0.0.{minor}'

                if self.component_bucket is None:
                    response = imagebuilder.create_component(
                        name=component_name,
                        semanticVersion=version,
                        description=component[component_name]['description'],
                        platform=platform,
                        data=yaml.safe_dump(component[component_name])
                    )
                else:
                    self.logger.info(f'Uploading Component {component_name} to S3')
                    buffer = StringIO()
                    yaml.safe_dump(component[component_name], buffer)
                    s3 = boto3.resource('s3')
                    key_name = f'imagebuilder/{component_name}_{datetime.now().time()}.yaml'
                    s3.Bucket(self.component_bucket).put_object(Key=key_name, Body=buffer.getvalue())


                    response = imagebuilder.create_component(
                        name=component_name,
                        semanticVersion=version,
                        description=component[component_name]['description'],
                        platform=platform,
                        uri=f's3://{self.component_bucket}/{key_name}'
                    )

                component_list.append({'componentArn': response['componentBuildVersionArn']})

        self.logger.debug(component_list)
        return component_list

    def create_image_recipe(self, component_arns, recipe_def):
        imagebuilder = boto3.client('imagebuilder')
        response = imagebuilder.list_image_recipes(owner='Self',
                                                   filters=[{'name': 'name',
                                                             'values': [recipe_def['name']]
                                                             }]
                                                   )
        minor = 1 + len(response['imageRecipeSummaryList'])
        version = f'0.0.{minor}'

        recipe_dict = {
            'name': recipe_def['name'],
            'description': recipe_def['description'],
            'components': component_arns,
            'semanticVersion': version,
            'parentImage': recipe_def['parentImage']
        }

        if recipe_def.get('blockDeviceMappings') is not None:
            recipe_dict['blockDeviceMappings'] = recipe_def['blockDeviceMappings']
        if recipe_def.get('tags') is not None:
            recipe_dict['tags'] = recipe_def['tags']

        response = imagebuilder.create_image_recipe(
            **recipe_dict
        )

        self.logger.debug(response['imageRecipeArn'])
        return response['imageRecipeArn']

    def create_infrastructure_config(self, instance_profile, infrastructure_def):
        imagebuilder = boto3.client('imagebuilder')
        response = imagebuilder.list_infrastructure_configurations(filters=[{'name': 'name',
                                                                             'values': [infrastructure_def['name']]
                                                                             }]
                                                                   )
        if len(response['infrastructureConfigurationSummaryList']) > 0:
            self.logger.warning('Infrastructure Configuration with this name already exists! Reusing')
            return response['infrastructureConfigurationSummaryList'][0]['arn']

        infrastructure_def['instanceProfileName'] = instance_profile
        response = imagebuilder.create_infrastructure_configuration(
            **infrastructure_def
        )

        self.logger.debug(response['infrastructureConfigurationArn'])
        return response['infrastructureConfigurationArn']

    def create_distribution_configuration(self, distribution_def):
        imagebuilder = boto3.client('imagebuilder')

        response = imagebuilder.list_distribution_configurations(filters=[{'name': 'name',
                                                                           'values': [distribution_def['name']]
                                                                           }]
                                                                 )
        if len(response['distributionConfigurationSummaryList']) > 0:
            self.logger.warning('Distribution Configuration with this name already exists! Reusing')
            return response['distributionConfigurationSummaryList'][0]['arn']

        response = imagebuilder.create_distribution_configuration(
            **distribution_def
        )

        self.logger.debug(response['distributionConfigurationArn'])
        return response['distributionConfigurationArn']

    def create_image_pipeline(self, pipeline_name, image_recipe_arn, infrastructure_arn, distribution_arn):
        imagebuilder = boto3.client('imagebuilder')

        response = imagebuilder.list_image_pipelines(filters=[{'name': 'name',
                                                               'values': [pipeline_name]
                                                               }]
                                                     )
        if len(response['imagePipelineList']) > 0:
            self.logger.warning('Image Pipeline with this name already exists! Recreating')
            imagebuilder.delete_image_pipeline(
                imagePipelineArn=response['imagePipelineList'][0]['arn']
            )

        response = imagebuilder.create_image_pipeline(
            name=pipeline_name,
            description=pipeline_name,
            imageRecipeArn=image_recipe_arn,
            infrastructureConfigurationArn=infrastructure_arn,
            distributionConfigurationArn=distribution_arn
        )

        self.logger.debug(response['imagePipelineArn'])
        return response['imagePipelineArn']

    @staticmethod
    def start_image_pipeline(pipeline_arn):
        imagebuilder = boto3.client('imagebuilder')
        response = imagebuilder.start_image_pipeline_execution(
            imagePipelineArn=pipeline_arn
        )

        return response['imageBuildVersionArn']

    def delete_pipeline_resources(self, pipeline_def):
        imagebuilder = boto3.client('imagebuilder')

        response = imagebuilder.list_image_pipelines(filters=[{'name': 'name',
                                                               'values': [pipeline_def['pipeline-name']]
                                                               }]
                                                     )
        if len(response['imagePipelineList']) > 0:
            self.logger.warning('Deleting Pipeline')
            imagebuilder.delete_image_pipeline(
                imagePipelineArn=response['imagePipelineList'][0]['arn']
            )
        response = imagebuilder.list_infrastructure_configurations(filters=[{'name': 'name',
                                                                             'values': [pipeline_def['infrastructure-configuration']['name']]
                                                                             }]
                                                                   )
        if len(response['infrastructureConfigurationSummaryList']) > 0:
            if self.update:
                self.logger.warning('Deleting Infrastructure Configuration')
                imagebuilder.delete_infrastructure_configuration(
                    infrastructureConfigurationArn=response['infrastructureConfigurationSummaryList'][0]['arn']
                )

        response = imagebuilder.list_distribution_configurations(filters=[{'name': 'name',
                                                                           'values': [pipeline_def['distribution-configuration']['name']]
                                                                           }]
                                                                 )
        if len(response['distributionConfigurationSummaryList']) > 0:
            if self.update:
                self.logger.warning('Deleting Distribution Configuration')
                imagebuilder.delete_distribution_configuration(
                    distributionConfigurationArn=response['distributionConfigurationSummaryList'][0]['arn']
                )


def main():
    build_pipeline = parseargs()
    build_pipeline.run()


if __name__ == '__main__':
    main()
