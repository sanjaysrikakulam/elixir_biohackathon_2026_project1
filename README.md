# Scalable BYOC in Galaxy and TREs across ELIXIR

[ELIXIR BioHackathon 2026](https://github.com/elixir-europe/biohackathon-projects-2026/blob/main/1.md)
[Galaxy Project](https://galaxyproject.org)

This repository contains the code, deployment configurations, and automation tools developed for **Project 1** of the [ELIXIR BioHackathon Europe 2026](https://github.com/elixir-europe/biohackathon-projects-2026/blob/main/1.md).

## 📖 Overview

Building on the capabilities of the **de.NBI Cloud** (SimpleVM) and prior work from **EOSC EuroScienceGateway**, this project introduces a unified framework to support advanced **Bring Your Own Compute (BYOC)** for Galaxy across ELIXIR Nodes and cloud providers. 

The framework bridges distributed infrastructures by supporting two primary operational models:
* **Fully Managed Scenarios:** Automated provisioning and integration of infrastructure components such as Pulsar, SLURM/HTCondor, and storage backends.
* **User-Managed Scenarios:** Flexible onboarding of external compute resources facilitated through Galaxy APIs and CI pipelines, maintaining reproducibility and ease of use.

Additionally, this project explores interoperability with Trusted Research Environments / Secure Processing Environments (TRE/SPE)—such as **SURF's Tinker SANE**—using reproducible VM images and web-based remote access to support sensitive data use cases.

---

## 📂 Repository Structure

The repository is organized into four main components:

* **`pulsar-byoc/`**: Configuration, deployment recipes, and templates for setting up scalable Pulsar execution backends to connect external compute resources to Galaxy.
* **`byoc-sync/`**: Synchronization utilities and automation scripts (e.g., leveraging Galaxy APIs/CI workflows) to register, update, and manage BYOC endpoints and destinations.
* **`fastapi-resenv/`**: A FastAPI-based service for managing, provisioning, or interfacing with portable, user-specific Virtual Research Environments (VREs) and TRE/SPE setups.
* **`pulsar-fastapi-deploy/`**: Deployment manifests and orchestration scripts combining Pulsar execution endpoints with the FastAPI research environment services.

---

## 🎯 Key Objectives & Outcomes

1. **Standardized BYOC Onboarding:** Simplify connecting external compute resources across different ELIXIR Nodes into Galaxy. 
2. **Scalable Execution:** Enable seamless scaling with Pulsar and SLURM​/HTCondor backends. 
3. **TRE / SPE Interoperability:** Investigate and prototype​ portable, secure, and user-specific research environments compatible with existing European solutions. 
