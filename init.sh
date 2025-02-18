ip=$(echo $1 | awk -F'@' '{ print $2 }')
echo $ip
scp ./k3s.sh "$1:/etc/rancher/k3s/k3s.yaml"
# ssh -t "$1" 'sh ./k3s.sh' && \
scp "$1:/etc/rancher/k3s/k3s.yaml" "$HOME/.kube/config"
sed -i "s/127.0.0.1/$ip/" "$HOME/.kube/config"
