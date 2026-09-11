# -*- mode: ruby -*-
# vi: set ft=ruby :
#
# Single Ubuntu virtual machine for the SNPedia Knowledge Exporter.
#
# Vagrant only creates the machine. All software (exporter, Prometheus,
# Grafana) is installed afterwards by Ansible running on the host:
#
#   vagrant up
#   cd ansible && ansible-playbook site.yml

Vagrant.require_version ">= 2.4.0"

VM_BOX      = "bento/ubuntu-24.04"
VM_HOSTNAME = "snpedia-monitor"
VM_IP       = "192.168.56.10"   # must match ansible_host in ansible/inventory.yml
VM_MEMORY   = 2048              # MB, enough for Prometheus and Grafana
VM_CPUS     = 2

Vagrant.configure("2") do |config|
  # The machine is intentionally not given a name with config.vm.define,
  # so Vagrant keeps the name "default" and the SSH key path used in
  # ansible/inventory.yml stays valid:
  #   .vagrant/machines/default/virtualbox/private_key
  config.vm.box          = VM_BOX
  config.vm.hostname     = VM_HOSTNAME
  config.vm.boot_timeout = 600

  # Host-only network. VirtualBox allows 192.168.56.0/21 by default.
  config.vm.network "private_network", ip: VM_IP

  # Files are copied by Ansible, so the shared folder is not needed.
  config.vm.synced_folder ".", "/vagrant", disabled: true

  config.vm.provider "virtualbox" do |vb|
    vb.name   = VM_HOSTNAME
    vb.memory = VM_MEMORY
    vb.cpus   = VM_CPUS
    vb.gui    = false
  end
end
