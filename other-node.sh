export INSTALL_K3S_EXEC="server --disable traefik --disable servicelb"
export K3S_KUBECONFIG_MODE="644"
export K3S_NODE_NAME=""
export K3S_TOKEN="K100a8d8233ae6e59262a8f3a5a68ed3905c7708ef60efa79445cd31afdd9345943::server:fb87ca7b9cfc8509e7f50272aaf27ecc"
export K3S_URL="https://192.168.1.117:6443"
curl -sfL https://get.k3s.io | sh -s -