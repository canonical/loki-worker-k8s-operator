# Loki Worker charm for Kubernetes

[![CharmHub Badge](https://charmhub.io/loki-worker-k8s/badge.svg)](https://charmhub.io/loki-worker-k8s)
[![Release](https://github.com/canonical/loki-worker-k8s-operator/actions/workflows/release.yaml/badge.svg)](https://github.com/canonical/loki-worker-k8s-operator/actions/workflows/release.yaml)
[![Discourse Status](https://img.shields.io/discourse/status?server=https%3A%2F%2Fdiscourse.charmhub.io&style=flat&label=CharmHub%20Discourse)](https://discourse.charmhub.io)

## Description

The Loki Worker charm provides a scalable long-term storage using [Loki](https://github.com/grafana/loki).
This charm is part of the Loki HA deployment, and is meant to be deployed together with the [loki-coordinator-k8s](https://github.com/canonical/loki-coordinator-k8s-operator) charm.

A Loki Worker can assume any role that the Loki binary can take on, and it should always be related to a Loki Coordinator.
