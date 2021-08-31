from pulpcore.app.serializers import repository
import json

from rest_framework import serializers
from django.core import exceptions

from guardian.shortcuts import get_users_with_perms

from pulp_container.app import models as container_models
from pulp_container.app import serializers as container_serializers
from pulpcore.plugin import models as core_models
from pulpcore.plugin import serializers as core_serializers

from galaxy_ng.app import models
from galaxy_ng.app.access_control import fields
from galaxy_ng.app.access_control.fields import GroupPermissionField, MyPermissionsField
from galaxy_ng.app.api.v3 import serializers as v3_serializers
from galaxy_ng.app.api import utils

namespace_fields = ("name", "my_permissions", "owners")


class ContainerNamespaceSerializer(serializers.ModelSerializer):
    my_permissions = MyPermissionsField(source="*", read_only=True)
    owners = serializers.SerializerMethodField()

    class Meta:
        model = models.ContainerNamespace
        fields = namespace_fields
        read_only_fields = (
            "name",
            "my_permissions",
        )

    def get_owners(self, namespace):
        return get_users_with_perms(namespace, with_group_users=False).values_list(
            "username", flat=True
        )


class ContainerNamespaceDetailSerializer(ContainerNamespaceSerializer):
    groups = GroupPermissionField()

    class Meta:
        model = models.ContainerNamespace
        fields = namespace_fields + ("groups",)
        read_only_fields = (
            "name",
            "my_permissions",
        )


class ContainerRepositorySerializer(serializers.ModelSerializer):
    pulp = serializers.SerializerMethodField()
    namespace = ContainerNamespaceSerializer()
    id = serializers.SerializerMethodField()
    created = serializers.SerializerMethodField()
    updated = serializers.SerializerMethodField()

    # This serializer is purposfully refraining from using pulp fields directly
    # in the top level response body. This is because future versions of hub will have to
    # support indexing other registries and the API responses for a container
    # repository should make sense for containers hosted by pulp and containers
    # hosted by other registries.
    class Meta:
        model = models.ContainerDistribution
        read_only_fields = (
            "id",
            "name",
            # this field will return null on instances where hub is indexing a
            # different repo
            "pulp",
            "namespace",
            "description",
            "created",
            "updated",
        )

        fields = read_only_fields

    def get_namespace(self, distro):
        return distro.namespace.name

    def get_id(self, distro):
        return distro.pk

    def get_created(self, distro):
        return distro.repository.pulp_created

    def get_updated(self, distro):
        return distro.repository.pulp_last_updated

    def get_pulp(self, distro):
        repo = distro.repository

        return {
            "repository": {
                "pulp_id": repo.pk,
                "pulp_type": repo.pulp_type,
                "version": repo.latest_version().number,
                "name": repo.name,
                "description": repo.description,
                "pulp_created": repo.pulp_created,
                "last_sync_task": _get_last_sync_task(repo),
                "pulp_labels": {label.key: label.value for label in repo.pulp_labels.all()},
            },
            "distribution": {
                "pulp_id": distro.pk,
                "name": distro.name,
                "pulp_created": distro.pulp_created,
                "base_path": distro.base_path,
                "pulp_labels": {label.key: label.value for label in distro.pulp_labels.all()},
            },
        }


def _get_last_sync_task(repo):
    sync_task = models.container.ContainerSyncTask.objects.filter(repository=repo).first()
    if not sync_task:
        # UI handles `null` as "no status"
        return

    return {
        "task_id": sync_task.pk,
        "state": sync_task.task.state,
        "started_at": sync_task.task.started_at,
        "finished_at": sync_task.task.finished_at,
        "error": sync_task.task.error,
    }


class ContainerManifestSerializer(serializers.ModelSerializer):
    config_blob = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()
    layers = serializers.SerializerMethodField()

    class Meta:
        model = container_models.Manifest
        fields = (
            "pulp_id",
            "digest",
            "schema_version",
            "media_type",
            "config_blob",
            "tags",
            "pulp_created",
            "layers",
        )

    def get_layers(self, obj):
        layers = []
        # use the prefetched blob_list and artifact_list instead of obj.blobs and
        # blob._artifacts to cut down on queries made.
        for blob in obj.blob_list:
            layers.append({"digest": blob.digest, "size": blob.artifact_list[0].size})

        return layers

    def get_config_blob(self, obj):
        return {"digest": obj.config_blob.digest, "media_type": obj.config_blob.media_type}

    def get_tags(self, obj):
        tags = []
        # tagget_manifests returns all tags on the manifest, not just the ones
        # that are in the latest version of the repo.
        for tag in obj.tagged_manifests.all():
            tags.append(tag.name)

        return tags


class ContainerManifestDetailSerializer(ContainerManifestSerializer):
    def get_config_blob(self, obj):
        with obj.config_blob._artifacts.first().file.open() as f:
            config_json = json.load(f)

        return {
            "digest": obj.config_blob.digest,
            "media_type": obj.config_blob.media_type,
            "data": config_json,
        }


class ContainerRepositoryHistorySerializer(serializers.ModelSerializer):
    added = serializers.SerializerMethodField()
    removed = serializers.SerializerMethodField()

    class Meta:
        model = core_models.RepositoryVersion
        fields = ("pulp_id", "added", "removed", "pulp_created", "number")

    def get_added(self, obj):
        return [self._content_info(content.content) for content in obj.added_memberships.all()]

    def get_removed(self, obj):
        return [self._content_info(content.content) for content in obj.removed_memberships.all()]

    def _content_info(self, content):
        return_data = {
            "pulp_id": content.pk,
            "pulp_type": content.pulp_type,
            "manifest_digest": None,
            "tag_name": None,
        }

        # TODO: Figure out if there is a way to prefetch Manifest and Tag objects
        if content.pulp_type == "container.manifest":
            manifest = container_models.Manifest.objects.get(pk=content.pk)
            return_data["manifest_digest"] = manifest.digest
        elif content.pulp_type == "container.tag":
            tag = container_models.Tag.objects.select_related("tagged_manifest").get(pk=content.pk)
            return_data["manifest_digest"] = tag.tagged_manifest.digest
            return_data["tag_name"] = tag.name

        return return_data


class ContainerReadmeSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.ContainerDistroReadme
        fields = (
            "updated",
            "created",
            "text",
        )

        read_only_fields = (
            "updated",
            "created",
        )


class ContainerRegistryRemoteSerializer(
    v3_serializers.LastSyncTaskMixin,
    core_serializers.RemoteSerializer,
):
    created_at = serializers.DateTimeField(source='pulp_created', required=False)
    updated_at = serializers.DateTimeField(source='pulp_last_updated', required=False)
    last_sync_task = serializers.SerializerMethodField()
    write_only_fields = serializers.SerializerMethodField()

    class Meta:
        model = models.ContainerRegistryRemote
        fields = [
            "pk",
            "name",
            "url",
            "policy",
            "created_at",
            "updated_at",
            "username",
            "password",
            "tls_validation",
            "client_key",
            "client_cert",
            "ca_cert",
            "last_sync_task",
            "download_concurrency",
            "proxy_url",
            "proxy_username",
            "proxy_password",
            "write_only_fields",
            "rate_limit",
        ]
        extra_kwargs = {
            'name': {'read_only': True},
            'client_key': {'write_only': True},
        }

    def get_write_only_fields(self, obj):
        return utils.get_write_only_fields(self, obj)

    def get_last_sync_task_queryset(self, obj):
        """Gets last_sync_task from Pulp using remote->repository relation"""

        return models.ContainerSyncTask.objects.filter(
            repository=obj.repository_set.order_by('-pulp_last_updated').first()).first()

class ContainerRemoteSerializer(
    container_serializers.ContainerRemoteSerializer,

):

    registry = serializers.SerializerMethodField()

    class Meta:
        model = container_models.ContainerRemote
        fields = [
            "pk",
            "name",
            "upstream_name",
            "registry",
        ]

    def get_registry(self, obj):
        try:
            return models.ContainerRegistryRepos.objects.get(repository_remote = obj.pk).registry.pk
        except exceptions.ObjectDoesNotExist:
            return None
