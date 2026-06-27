import PropTypes from "prop-types";
import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import CircularProgress from "@mui/material/CircularProgress";
import Iconify from "src/components/iconify";
import { useCapabilities } from "src/hooks/useCapabilities";

const CONTACT_URL = "https://futureagi.com/talk-to-human";

export default function CapabilityGate({ feature, children }) {
  const { data, isLoading } = useCapabilities();

  if (isLoading) {
    return (
      <Stack
        alignItems="center"
        justifyContent="center"
        sx={{ height: 1, minHeight: 240 }}
      >
        <CircularProgress size={32} />
      </Stack>
    );
  }

  const featureData = data?.features?.[feature];
  const allowed = featureData?.allowed === true;

  if (allowed) {
    return children;
  }

  const reasonCode = featureData?.reason_code;

  return (
    <Stack
      alignItems="center"
      justifyContent="center"
      spacing={2}
      sx={{ height: 1, minHeight: 480, px: 3, textAlign: "center" }}
    >
      <Iconify
        icon="mdi:rocket-launch-outline"
        sx={{ width: 64, height: 64, color: "primary.main" }}
      />
      <Typography variant="h5">This feature requires an upgrade.</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ maxWidth: 480 }}>
        {reasonCode
          ? `Access to "${feature}" is restricted: ${reasonCode}.`
          : `"${feature}" is not included in your current license. Upgrade to unlock this feature.`}
      </Typography>
      <Button
        variant="contained"
        color="primary"
        href={CONTACT_URL}
        target="_blank"
        rel="noopener"
      >
        Contact us to upgrade
      </Button>
    </Stack>
  );
}

CapabilityGate.propTypes = {
  feature: PropTypes.string.isRequired,
  children: PropTypes.node,
};
